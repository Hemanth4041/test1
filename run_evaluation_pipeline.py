"""
Agent evaluation pipeline.

Two modes:
  1. Session ID mode  — fetch an existing session's responses from Vertex AI
                        and score them against the golden dataset.
  2. Live mode        — create a fresh session, drive it with the golden
                        dataset's user messages, and score the responses.

The golden dataset lives at:
  data/1777463419356577792_golden_dataset.json
"""

import json
import datetime
from typing import Dict, List, Any, Optional

from google.cloud import bigquery
import google.cloud.aiplatform_v1beta1 as aiplatform_v1b
import vertexai
import google.auth
from google.auth.transport.requests import Request

from config import EvaluationConfig

vertexai.init(project=EvaluationConfig.PROJECT_ID, location=EvaluationConfig.LOCATION)
bq_client = bigquery.Client(project=EvaluationConfig.PROJECT_ID)

RESOURCE_NAME = EvaluationConfig.get_resource_name()


credentials = None
session_client = None
execution_client = None


# ---------------------------------------------------------------------------
# INITIALISATION
# ---------------------------------------------------------------------------

def initialize_agent() -> bool:
    global credentials, session_client, execution_client
    try:
        creds, _ = google.auth.default()
        creds.refresh(Request())
        credentials = creds

        api_endpoint = f"{EvaluationConfig.LOCATION}-aiplatform.googleapis.com"
        client_options = {"api_endpoint": api_endpoint}

        session_client = aiplatform_v1b.SessionServiceClient(client_options=client_options)
        execution_client = aiplatform_v1b.ReasoningEngineExecutionServiceClient(
            client_options=client_options
        )
        return True
    except Exception as e:
        print(f"Error initialising agent client: {e}")
        return False


# ---------------------------------------------------------------------------
# GOLDEN DATASET
# ---------------------------------------------------------------------------

def load_golden_dataset() -> Optional[Dict[str, Any]]:
    """Load the fixed golden dataset from the data folder."""
    try:
        with open(EvaluationConfig.get_golden_dataset_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: golden dataset not found at {EvaluationConfig.get_golden_dataset_path()}")
        return None
    except json.JSONDecodeError:
        print(f"Error: golden dataset at {EvaluationConfig.get_golden_dataset_path()} is not valid JSON.")
        return None


# ---------------------------------------------------------------------------
# MODE 1 — SESSION ID: fetch existing responses and score against golden
# ---------------------------------------------------------------------------

def fetch_session_events(session_id: str) -> List[Any]:
    """Fetch all raw events for a session from Vertex AI."""
    session_resource = f"{RESOURCE_NAME}/sessions/{session_id}"
    try:
        events = session_client.list_events(
            request=aiplatform_v1b.ListEventsRequest(parent=session_resource)
        )
        return list(events)
    except Exception as e:
        print(f"Error fetching session events: {e}")
        return []


def _extract_text(parts: List[Any]) -> str:
    texts = []
    for part in parts:
        if isinstance(part, dict):
            if "text" in part:
                texts.append(part["text"])
        elif hasattr(part, "text") and part.text:
            texts.append(part.text)
    return " ".join(texts).strip()


def _extract_tool_calls_from_parts(parts: List[Any]) -> List[Dict[str, Any]]:
    tool_calls = []
    for part in parts:
        fc = None
        if isinstance(part, dict):
            fc = part.get("functionCall") or part.get("function_call")
        elif hasattr(part, "function_call") and part.function_call:
            fc = part.function_call

        if fc:
            if isinstance(fc, dict):
                tool_calls.append({
                    "tool_name": fc.get("name", "unknown"),
                    "parameters": fc.get("args", {}),
                })
            else:
                tool_calls.append({
                    "tool_name": getattr(fc, "name", "unknown"),
                    "parameters": dict(getattr(fc, "args", {}) or {}),
                })
    return tool_calls


# Author strings that indicate a human/user turn
_USER_ROLES = {"user", "human", "1"}
# Author strings that indicate an agent/model turn  
_AGENT_ROLES = {"agent", "model", "assistant", "0"}


def fetch_session_responses(session_id: str) -> List[Dict[str, Any]]:
    """
    Fetch a session's event history and return a list of turn dicts:
      { "user_message": ..., "agent_response": ..., "tool_calls": [...] }
    ordered by the conversation sequence.
    """
    events = fetch_session_events(session_id)
    if not events:
        return []

    print(f"  Raw events found: {len(events)}")

    turns = []
    pending_user: Optional[str] = None

    for idx, event in enumerate(events):
        # Normalise: SDK objects vs plain dicts
        if hasattr(event, "author"):
            raw_role = str(event.author)
            parts = list(event.content.parts) if hasattr(event.content, "parts") else []
        elif isinstance(event, dict):
            raw_role = str(event.get("author", event.get("role", "")))
            parts = event.get("content", {}).get("parts", [])
        else:
            print(f"  Event {idx}: unrecognised format {type(event)}")
            continue

        role = raw_role.lower().strip()
        text = _extract_text(parts)
        print(f"  Event {idx}: author='{raw_role}'  text_preview='{text[:80]}'")

        if role in _USER_ROLES or "user" in role or "human" in role:
            pending_user = text

        elif (role in _AGENT_ROLES or "agent" in role or "model" in role or "assistant" in role)                 and pending_user is not None:
            turns.append({
                "user_message": pending_user,
                "agent_response": text,
                "tool_calls": _extract_tool_calls_from_parts(parts),
            })
            pending_user = None

    print(f"  Paired turns extracted: {len(turns)}")
    return turns


def evaluate_session_id(session_id: str, golden_test_case: Dict) -> Dict[str, Any]:
    """
    Score the responses already recorded in an existing session
    against the golden dataset's expected outputs.
    """
    golden_turns = golden_test_case["turns"]
    session_turns = fetch_session_responses(session_id)

    if not session_turns:
        print(f"No responses found in session {session_id}.")
        metrics = _zero_metrics()
        metrics["session_id"] = session_id
        return metrics

    total_turns = len(golden_turns)
    tool_scores, response_scores, safety_scores, groundedness_scores = [], [], [], []
    successful_turns = 0
    perfect_tool_turns = 0

    for i, golden_turn in enumerate(golden_turns):
        # Match session turns by position; pad with empty if session is shorter
        if i < len(session_turns):
            actual_text  = session_turns[i]["agent_response"]
            actual_tools = session_turns[i]["tool_calls"]
        else:
            actual_text  = ""
            actual_tools = []

        exp_tools            = golden_turn.get("expected_tool_calls", [])
        exp_pattern          = golden_turn.get("expected_response_pattern", {})
        exp_response         = golden_turn.get("expected_response", "")

        t_score = _score_tool_calls(exp_tools, actual_tools)
        r_score = _score_response(exp_response, actual_text, exp_pattern)
        s_score = _score_safety(actual_text)
        g_score = _score_groundedness(actual_text, exp_response)

        tool_scores.append(t_score)
        response_scores.append(r_score)
        safety_scores.append(s_score)
        groundedness_scores.append(g_score)

        if t_score == 1.0:
            perfect_tool_turns += 1
        if actual_tools or actual_text:
            successful_turns += 1

        # Per-turn debug output
        print(f"\n  {'─' * 60}")
        print(f"  Turn {i + 1}/{total_turns}")
        print(f"  User             : {golden_turn['user_message']}")
        print(f"  Expected response: {exp_response[:200]}{'...' if len(exp_response) > 200 else ''}")
        print(f"  Expected tools   : {[t['tool_name'] for t in exp_tools]}")
        print(f"  Actual response  : {actual_text[:200]}{'...' if len(actual_text) > 200 else ''}")
        print(f"  Actual tools     : {[t['tool_name'] for t in actual_tools]}")
        print(f"  Scores → tool: {t_score:.2f}  response: {r_score:.2f}  safety: {s_score:.2f}  groundedness: {g_score:.2f}")

    return _build_metrics(
        session_id=session_id,
        total_turns=total_turns,
        tool_scores=tool_scores,
        response_scores=response_scores,
        safety_scores=safety_scores,
        groundedness_scores=groundedness_scores,
        successful_turns=successful_turns,
        perfect_tool_turns=perfect_tool_turns,
    )


# ---------------------------------------------------------------------------
# MODE 2 — LIVE SESSION: drive a fresh session and score against golden
# ---------------------------------------------------------------------------

def create_session() -> Optional[str]:
    try:
        operation = session_client.create_session(
            parent=RESOURCE_NAME,
            session=aiplatform_v1b.Session(user_id=EvaluationConfig.EVAL_USER_ID),
        )
        session = operation.result(timeout=60)
        return session.name
    except Exception as e:
        print(f"Error creating session: {e}")
        return None


def call_agent(user_message: str, session_name: str) -> Dict[str, Any]:
    try:
        credentials.refresh(Request())

        bare_session_id = session_name.split("/")[-1]
        input_payload = {
            "message": user_message,
            "user_id": EvaluationConfig.EVAL_USER_ID,
            "session_id": bare_session_id,
        }

        chunks = []
        for chunk in execution_client.stream_query_reasoning_engine(
            request=aiplatform_v1b.StreamQueryReasoningEngineRequest(
                name=RESOURCE_NAME,
                input=input_payload,
            )
        ):
            try:
                parsed = json.loads(chunk.data)
                if isinstance(parsed, list):
                    chunks.extend(parsed)
                else:
                    chunks.append(parsed)
            except (json.JSONDecodeError, AttributeError):
                pass

        return _aggregate_streaming_response(chunks)
    except Exception as e:
        return {"error": str(e)}


def _aggregate_streaming_response(chunks: List[Any]) -> Dict[str, Any]:
    text_parts, tool_calls = [], []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        if "content" in chunk and "parts" in chunk.get("content", {}):
            for part in chunk["content"]["parts"]:
                if "text" in part:
                    text_parts.append(part["text"])
        elif "text" in chunk:
            text_parts.append(chunk["text"])
        elif "output" in chunk:
            text_parts.append(str(chunk["output"]))

        for key in ("tool_calls", "toolCalls"):
            tool_calls.extend(chunk.get(key, []))

    return {
        "response": "".join(text_parts),
        "tool_calls": tool_calls,
        "all_chunks": chunks,
    }


def extract_tool_calls(agent_response: Dict) -> List[Dict]:
    tool_calls = []

    for tc in agent_response.get("tool_calls", []):
        tool_calls.append({
            "tool_name": tc.get("name", tc.get("tool_name", "unknown")),
            "parameters": tc.get("parameters", tc.get("args", {})),
        })

    for chunk in agent_response.get("all_chunks", []):
        if not isinstance(chunk, dict):
            continue

        if "functionCall" in chunk:
            tool_calls.append({
                "tool_name": chunk["functionCall"].get("name", "unknown"),
                "parameters": chunk["functionCall"].get("args", {}),
            })

        for part in chunk.get("content", {}).get("parts", []):
            for key in ("functionCall", "function_call"):
                if key in part:
                    tool_calls.append({
                        "tool_name": part[key].get("name", "unknown"),
                        "parameters": part[key].get("args", {}),
                    })

    seen, unique = set(), []
    for tc in tool_calls:
        sig = (tc["tool_name"], json.dumps(tc["parameters"], sort_keys=True))
        if sig not in seen:
            seen.add(sig)
            unique.append(tc)
    return unique


def evaluate_live_session(golden_test_case: Dict) -> Dict[str, Any]:
    """
    Create a fresh agent session, send each golden turn's user message,
    and score the live responses against the golden expected outputs.
    """
    golden_turns = golden_test_case["turns"]
    total_turns  = len(golden_turns)

    session_name = create_session()
    if not session_name:
        metrics = _zero_metrics()
        metrics["session_id"] = "N/A"
        return metrics

    bare_session_id = session_name.split("/")[-1]
    print(f"  Live session created: {bare_session_id}")

    tool_scores, response_scores, safety_scores, groundedness_scores = [], [], [], []
    successful_turns  = 0
    perfect_tool_turns = 0

    for i, golden_turn in enumerate(golden_turns):
        user_message = golden_turn["user_message"]
        print(f"  Turn {i + 1}/{total_turns}: {user_message[:60]}...")

        resp = call_agent(user_message, session_name)

        if "error" in resp:
            tool_scores.append(0.0)
            response_scores.append(0.0)
            safety_scores.append(1.0)
            groundedness_scores.append(0.0)
            print(f"\n  {'─' * 60}")
            print(f"  Turn {i + 1}/{total_turns}")
            print(f"  User    : {golden_turn['user_message']}")
            print(f"  ERROR   : {resp['error']}")
            continue

        actual_tools = extract_tool_calls(resp)
        actual_text  = resp.get("response", "")

        exp_tools            = golden_turn.get("expected_tool_calls", [])
        exp_pattern          = golden_turn.get("expected_response_pattern", {})
        exp_response         = golden_turn.get("expected_response", "")

        t_score = _score_tool_calls(exp_tools, actual_tools)
        r_score = _score_response(exp_response, actual_text, exp_pattern)
        s_score = _score_safety(actual_text)
        g_score = _score_groundedness(actual_text, exp_response)

        tool_scores.append(t_score)
        response_scores.append(r_score)
        safety_scores.append(s_score)
        groundedness_scores.append(g_score)

        if t_score == 1.0:
            perfect_tool_turns += 1
        if actual_tools or actual_text:
            successful_turns += 1

        # Per-turn debug output
        print(f"\n  {'─' * 60}")
        print(f"  Turn {i + 1}/{total_turns}")
        print(f"  User             : {golden_turn['user_message']}")
        print(f"  Expected response: {exp_response[:200]}{'...' if len(exp_response) > 200 else ''}")
        print(f"  Expected tools   : {[t['tool_name'] for t in exp_tools]}")
        print(f"  Actual response  : {actual_text[:200]}{'...' if len(actual_text) > 200 else ''}")
        print(f"  Actual tools     : {[t['tool_name'] for t in actual_tools]}")
        print(f"  Scores → tool: {t_score:.2f}  response: {r_score:.2f}  safety: {s_score:.2f}  groundedness: {g_score:.2f}")

    return _build_metrics(
        session_id=bare_session_id,
        total_turns=total_turns,
        tool_scores=tool_scores,
        response_scores=response_scores,
        safety_scores=safety_scores,
        groundedness_scores=groundedness_scores,
        successful_turns=successful_turns,
        perfect_tool_turns=perfect_tool_turns,
    )


# ---------------------------------------------------------------------------
# SCORING HELPERS
# ---------------------------------------------------------------------------

def _score_tool_calls(expected: List[Dict], actual: List[Dict]) -> float:
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    matches = 0
    for et in expected:
        for at in actual:
            if at["tool_name"] == et["tool_name"]:
                ep = et.get("parameters", {})
                ap = at.get("parameters", {})
                if all(ap.get(k) == v for k, v in ep.items()):
                    matches += 1
                    break
    return matches / len(expected)


def _score_response(expected_response: str, actual: str, pattern: Dict) -> float:
    """
    Score the actual response against the golden expected_response.

    Primary:  keyword overlap between expected_response words and actual response.
    Secondary: must_not_contain check from pattern (hard fail if triggered).
    Keywords in pattern are only used as a similarity hint, not as a gate.
    """
    if not actual:
        return 0.0

    lower_actual = actual.lower()

    # Hard fail on banned phrases
    for bad in pattern.get("must_not_contain", []):
        if bad.lower() in lower_actual:
            return 0.0

    # If no expected response, fall back to length heuristic
    if not expected_response:
        return 0.8 if len(actual) > 50 else 0.5

    # Keyword overlap: meaningful words (>3 chars) from expected_response vs actual
    expected_words = [
        w.strip(".,!?|:()[]#*-").lower()
        for w in expected_response.split()
        if len(w.strip(".,!?|:()[]#*-")) > 3
    ]
    if not expected_words:
        return 0.8 if len(actual) > 50 else 0.5

    matched = sum(1 for w in expected_words if w in lower_actual)
    return matched / len(expected_words)


def _score_safety(actual: str) -> float:
    if not actual:
        return 1.0
    for word in EvaluationConfig.UNSAFE_KEYWORDS:
        if word in actual.lower():
            return 0.0
    return 1.0


def _score_groundedness(actual: str, expected_response: str) -> float:
    """
    Checks whether the actual response is grounded in the expected response
    by measuring how many key facts/terms from the expected response appear
    in the actual response.
    """
    if not actual:
        return 0.0
    if not expected_response:
        return 1.0
    # Use meaningful words (>4 chars) from expected_response as grounding anchors
    anchors = [
        w.strip(".,!?|:()[]#*-").lower()
        for w in expected_response.split()
        if len(w.strip(".,!?|:()[]#*-")) > 4
    ]
    if not anchors:
        return 1.0
    lower_actual = actual.lower()
    found = sum(1 for w in anchors if w in lower_actual)
    return found / len(anchors)


def _zero_metrics() -> Dict[str, float]:
    return {k: 0.0 for k in [
        "tool_trajectory_avg_score",
        "response_match_score",
        "groundedness_v1",
        "safety_v1",
        "multi_turn_task_success_v1",
        "multi_turn_trajectory_quality_v1",
        "multi_turn_tool_use_quality_v1",
        "final_response_match_v2",
    ]}


def _build_metrics(
    session_id: str,
    total_turns: int,
    tool_scores: List[float],
    response_scores: List[float],
    safety_scores: List[float],
    groundedness_scores: List[float],
    successful_turns: int,
    perfect_tool_turns: int,
) -> Dict[str, Any]:
    sr  = successful_turns / total_turns if total_turns else 0.0
    at  = sum(tool_scores) / total_turns if total_turns else 0.0
    ar  = sum(response_scores) / total_turns if total_turns else 0.0
    avg_safety       = sum(safety_scores) / total_turns if total_turns else 1.0
    avg_groundedness = sum(groundedness_scores) / total_turns if total_turns else 0.0

    return {
        "session_id": session_id,
        "tool_trajectory_avg_score": at,
        "response_match_score": ar,
        "groundedness_v1": avg_groundedness,
        "safety_v1": avg_safety,
        "multi_turn_task_success_v1": sr,
        "multi_turn_trajectory_quality_v1": (sr + at + ar) / 3.0,
        "multi_turn_tool_use_quality_v1": perfect_tool_turns / total_turns if total_turns else 0.0,
        "final_response_match_v2": response_scores[-1] if response_scores else 0.0,
    }


# ---------------------------------------------------------------------------
# BIGQUERY
# ---------------------------------------------------------------------------

def calculate_aggregate_metrics(all_metrics: List[Dict]) -> Dict[str, Any]:
    if not all_metrics:
        return {}

    metric_keys = list(_zero_metrics().keys())
    agg = {
        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_endpoint": RESOURCE_NAME,
        "total_test_cases": len(all_metrics),
    }

    for k in metric_keys:
        vals = [m[k] for m in all_metrics if k in m]
        agg[k] = sum(vals) / len(vals) if vals else 0.0

    thresholds = EvaluationConfig.load_eval_criteria()
    is_failing = any(
        agg.get(k, 0.0) < threshold * 0.75
        for k, threshold in thresholds.items()
    )
    agg["overall_status"] = "FAILED" if is_failing else "PASSED"
    return agg


def record_snapshot(metrics: Dict[str, Any], all_session_metrics: List[Dict]) -> None:
    table_ref = (
        f"{EvaluationConfig.PROJECT_ID}."
        f"{EvaluationConfig.BQ_DATASET}."
        f"{EvaluationConfig.BQ_TABLE}"
    )

    rows = [
        {
            "execution_timestamp": metrics["execution_timestamp"],
            "session_id": m.get("session_id", "unknown"),
            "agent_endpoint": metrics["agent_endpoint"],
            "response_match_score": float(m.get("response_match_score", 0.0)),
            "safety_v1": float(m.get("safety_v1", 1.0)),
            "multi_turn_task_success_v1": float(m.get("multi_turn_task_success_v1", 0.0)),
            "multi_turn_trajectory_quality_v1": float(m.get("multi_turn_trajectory_quality_v1", 0.0)),
            "multi_turn_tool_use_quality_v1": float(m.get("multi_turn_tool_use_quality_v1", 0.0)),
            "final_response_match_v2": float(m.get("final_response_match_v2", 0.0)),
            "tool_trajectory_avg_score": float(m.get("tool_trajectory_avg_score", 0.0)),
            "groundedness_v1": float(m.get("groundedness_v1", 0.0)),
            "overall_status": metrics.get("overall_status", "FAILED"),
        }
        for m in all_session_metrics
    ]

    bq_client.insert_rows_json(table_ref, rows)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    print("Agent Evaluation Pipeline")
    print("-" * 40)

    # Load golden dataset first — required for both modes
    golden_data = load_golden_dataset()
    if not golden_data:
        return

    test_cases = golden_data.get("test_cases", [])
    if not test_cases:
        print("Error: no test cases found in the golden dataset.")
        return

    # Use first test case (there is only one in the golden dataset)
    golden_test_case = test_cases[0]
    print(f"Golden dataset loaded: {golden_test_case.get('test_case_id', 'unknown')}")
    print(f"  {len(golden_test_case['turns'])} turns, description: {golden_test_case.get('description', '')}")
    print()

    # Ask user which mode
    session_id = input(
        "Enter a session ID to evaluate an existing session,\n"
        "or press Enter to run a live session: "
    ).strip()

    print()

    if not initialize_agent():
        print("Error: could not initialise the agent client.")
        return

    if session_id:
        # Mode 1: evaluate existing session against golden dataset
        print(f"Fetching responses from session {session_id}...")
        metrics = evaluate_session_id(session_id, golden_test_case)
    else:
        # Mode 2: run a fresh live session and evaluate against golden dataset
        print("Starting live session evaluation...")
        metrics = evaluate_live_session(golden_test_case)

    agg = calculate_aggregate_metrics([metrics])
    record_snapshot(agg, [metrics])

    print()
    print("Evaluation complete.")
    print(f"  Session ID     : {metrics['session_id']}")
    print(f"  Overall status : {agg['overall_status']}")
    print(f"  Tool score     : {metrics['tool_trajectory_avg_score']:.2f}")
    print(f"  Response score : {metrics['response_match_score']:.2f}")
    print(f"  Safety         : {metrics['safety_v1']:.2f}")
    print(f"  Task success   : {metrics['multi_turn_task_success_v1']:.2f}")
    print()
    print("Results recorded in BigQuery.")


if __name__ == "__main__":
    run_pipeline()
