"""
Agent evaluation pipeline.

Two modes:
  1. Session ID mode  — fetch an existing session's responses from Vertex AI and score.
  2. Live mode        — create a fresh session, drive it with the golden dataset, and score.
"""

import json
import re
import datetime
import sys
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

# Global criteria placeholder initialized dynamically at runtime
EVAL_CRITERIA: Dict[str, float] = {}

credentials      = None
session_client   = None
execution_client = None

# ── INITIALISATION ────────────────────────────────────────────────────────────

def initialize_agent() -> bool:
    global credentials, session_client, execution_client
    try:
        creds, _ = google.auth.default()
        creds.refresh(Request())
        credentials = creds

        api_endpoint   = f"{EvaluationConfig.LOCATION}-aiplatform.googleapis.com"
        client_options = {"api_endpoint": api_endpoint}

        session_client   = aiplatform_v1b.SessionServiceClient(client_options=client_options)
        execution_client = aiplatform_v1b.ReasoningEngineExecutionServiceClient(
            client_options=client_options
        )
        return True
    except Exception as e:
        print(f"Error initializing agent client: {e}", file=sys.stderr)
        return False


# ── GOLDEN DATASET ────────────────────────────────────────────────────────────

def load_golden_dataset() -> Optional[Dict[str, Any]]:
    try:
        with open(EvaluationConfig.get_golden_dataset_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Golden dataset not found at {EvaluationConfig.get_golden_dataset_path()}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print("Golden dataset is not valid JSON.", file=sys.stderr)
        return None


# ── SESSION EVENT EXTRACTION ──────────────────────────────────────────────────

def _role_type(raw_role: str) -> str:
    r = raw_role.lower().strip()
    if r in EvaluationConfig.USER_ROLES or "user" in r or "human" in r: 
        return "user"
    if r in EvaluationConfig.TOOL_ROLES or "tool" in r or "function" in r: 
        return "tool"
    if r in EvaluationConfig.AGENT_ROLES or "model" in r or "agent" in r or "assistant" in r: 
        return "agent"
    return "unknown"

def _parts_from_event(event: Any) -> List[Any]:
    if hasattr(event, "content"):
        content = event.content
        if hasattr(content, "parts"):
            return list(content.parts)
    if isinstance(event, dict):
        return event.get("content", {}).get("parts", [])
    return []

def _raw_role(event: Any) -> str:
    if hasattr(event, "author"):
        return str(event.author)
    if isinstance(event, dict):
        return str(event.get("author", event.get("role", "")))
    return ""

def _extract_text(parts: List[Any]) -> str:
    texts = []
    for part in parts:
        if isinstance(part, dict):
            t = part.get("text", "")
            if t:
                texts.append(t)
        elif hasattr(part, "text") and part.text:
            texts.append(part.text)
    return " ".join(texts).strip()

def _extract_tool_calls(parts: List[Any]) -> List[Dict[str, Any]]:
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
                    "tool_name":  fc.get("name", "unknown"),
                    "parameters": fc.get("args", {}),
                })
            else:
                tool_calls.append({
                    "tool_name":  getattr(fc, "name", "unknown"),
                    "parameters": dict(getattr(fc, "args", {}) or {}),
                })
    return tool_calls

def fetch_session_events(session_id: str) -> List[Any]:
    session_resource = f"{RESOURCE_NAME}/sessions/{session_id}"
    try:
        events = session_client.list_events(
            request=aiplatform_v1b.ListEventsRequest(parent=session_resource)
        )
        return list(events)
    except Exception as e:
        print(f"Error fetching session events: {e}", file=sys.stderr)
        return []

def fetch_session_responses(session_id: str) -> List[Dict[str, Any]]:
    events = fetch_session_events(session_id)
    if not events:
        return []

    turns: List[Dict[str, Any]] = []
    pending_user  : Optional[str]        = None
    pending_text  : List[str]            = []
    pending_tools : List[Dict[str, Any]] = []

    def _flush() -> None:
        if pending_user is not None:
            turns.append({
                "user_message":   pending_user,
                "agent_response": " ".join(pending_text).strip(),
                "tool_calls":     list(pending_tools),
            })

    for event in events:
        rr    = _raw_role(event)
        kind  = _role_type(rr)
        parts = _parts_from_event(event)
        text  = _extract_text(parts)
        tools = _extract_tool_calls(parts)

        if kind == "user":
            _flush()
            pending_user  = text
            pending_text  = []
            pending_tools = []
        elif kind == "agent":
            if text:
                pending_text.append(text)
            pending_tools.extend(tools)

    _flush()
    return turns


# ── EVALUATION MODES ──────────────────────────────────────────────────────────

def evaluate_session_id(session_id: str, golden_test_case: Dict) -> Dict[str, Any]:
    golden_turns  = golden_test_case["turns"]
    session_turns = fetch_session_responses(session_id)

    if not session_turns:
        print(f"Error: No turns could be extracted from session {session_id}.", file=sys.stderr)
        m = _zero_metrics()
        m["session_id"] = session_id
        return m

    return _score_turns(
        session_id=session_id,
        golden_turns=golden_turns,
        actual_turns=session_turns,
        name_only_tool_scoring=True,
    )


def create_session() -> Optional[str]:
    try:
        op = session_client.create_session(
            parent=RESOURCE_NAME,
            session=aiplatform_v1b.Session(user_id=EvaluationConfig.EVAL_USER_ID),
        )
        session = op.result(timeout=60)
        return session.name
    except Exception as e:
        print(f"Error creating session: {e}", file=sys.stderr)
        return None


def call_agent(user_message: str, session_name: str) -> Dict[str, Any]:
    try:
        credentials.refresh(Request())
        bare_session_id = session_name.split("/")[-1]

        chunks: List[Any] = []
        for chunk in execution_client.stream_query_reasoning_engine(
            request=aiplatform_v1b.StreamQueryReasoningEngineRequest(
                name=RESOURCE_NAME,
                input={
                    "message":    user_message,
                    "user_id":    EvaluationConfig.EVAL_USER_ID,
                    "session_id": bare_session_id,
                },
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
    text_parts: List[str]  = []
    tool_calls: List[Dict] = []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if "content" in chunk and "parts" in chunk.get("content", {}):
            for part in chunk["content"]["parts"]:
                if "text" in part:
                    text_parts.append(part["text"])
                for key in ("functionCall", "function_call"):
                    if key in part:
                        tool_calls.append({
                            "tool_name":  part[key].get("name", "unknown"),
                            "parameters": part[key].get("args", {}),
                        })
        elif "text" in chunk:
            text_parts.append(chunk["text"])
        elif "output" in chunk:
            text_parts.append(str(chunk["output"]))

        for key in ("tool_calls", "toolCalls"):
            for tc in chunk.get(key, []):
                tool_calls.append({
                    "tool_name":  tc.get("name", tc.get("tool_name", "unknown")),
                    "parameters": tc.get("parameters", tc.get("args", {})),
                })

        if "functionCall" in chunk:
            tool_calls.append({
                "tool_name":  chunk["functionCall"].get("name", "unknown"),
                "parameters": chunk["functionCall"].get("args", {}),
            })

    seen, unique = set(), []
    for tc in tool_calls:
        sig = (tc["tool_name"], json.dumps(tc["parameters"], sort_keys=True))
        if sig not in seen:
            seen.add(sig)
            unique.append(tc)

    return {
        "response":   "".join(text_parts),
        "tool_calls": unique,
        "all_chunks": chunks,
    }


def evaluate_live_session(golden_test_case: Dict) -> Dict[str, Any]:
    golden_turns = golden_test_case["turns"]
    session_name = create_session()
    if not session_name:
        m = _zero_metrics()
        m["session_id"] = "N/A"
        return m

    bare_session_id = session_name.split("/")[-1]
    actual_turns: List[Dict] = []

    for golden_turn in golden_turns:
        msg = golden_turn["user_message"]
        resp = call_agent(msg, session_name)

        if "error" in resp:
            actual_turns.append({
                "user_message":   msg,
                "agent_response": "",
                "tool_calls":     [],
                "error":          resp["error"],
            })
        else:
            actual_turns.append({
                "user_message":   msg,
                "agent_response": resp.get("response", ""),
                "tool_calls":     resp.get("tool_calls", []),
            })

    return _score_turns(
        session_id=bare_session_id,
        golden_turns=golden_turns,
        actual_turns=actual_turns,
        name_only_tool_scoring=False,
    )


# ── CORE SCORING ENGINE ───────────────────────────────────────────────────────

def _score_turns(
    session_id: str,
    golden_turns: List[Dict],
    actual_turns: List[Dict],
    name_only_tool_scoring: bool = False,
    debug_file_path: str = "evaluation_debug.txt"
) -> Dict[str, Any]:
    total_turns = len(golden_turns)
    tool_scores, response_scores, safety_scores, groundedness_scores = [], [], [], []
    successful_turns   = 0
    perfect_tool_turns = 0

    # Write debug information to file instead of stdout
    with open(debug_file_path, "w", encoding="utf-8") as dbg:
        print("\n" + "=" * 60, file=dbg)
        print(f" LIVE AGENT EVALUATION RUN & COMPARISON ", file=dbg)
        print(f" Session ID: {session_id}", file=dbg)
        print("=" * 60, file=dbg)

        for i, golden_turn in enumerate(golden_turns):
            actual       = actual_turns[i] if i < len(actual_turns) else {}
            actual_text  = actual.get("agent_response", "")
            actual_tools = actual.get("tool_calls", [])

            exp_tools    = golden_turn.get("expected_tool_calls", [])
            exp_pattern  = golden_turn.get("expected_response_pattern", {})
            exp_response = golden_turn.get("expected_response", "")
            user_message = golden_turn.get("user_message", "")

            t_score = _score_tool_calls(exp_tools, actual_tools, name_only=name_only_tool_scoring)
            r_score = _score_response(exp_response, actual_text, exp_pattern)
            s_score = _score_safety(actual_text)
            g_score = _score_groundedness(actual_text)

            tool_scores.append(t_score)
            response_scores.append(r_score)
            safety_scores.append(s_score)
            groundedness_scores.append(g_score)

            if t_score >= 1.0:
                perfect_tool_turns += 1
            if actual_tools or actual_text:
                successful_turns += 1

            # Print detailed visual evaluation mapping for the ongoing turn to the text file
            print(f"\n[TURN {i + 1} / {total_turns}]", file=dbg)
            print(f"  User Input:        {user_message}", file=dbg)
            print(f"  ── Response Verification ──", file=dbg)
            print(f"    Expected Match:  {exp_response if exp_response else '[Pattern Matching Only]'}", file=dbg)
            print(f"    Agent Response:  {actual_text if actual_text else '[Empty response or error state]'}", file=dbg)
            if exp_pattern.get("must_not_contain"):
                print(f"    Negative Filter: Must NOT contain: {exp_pattern['must_not_contain']}", file=dbg)
            print(f"  ── Tool Call Trajectory ──", file=dbg)
            print(f"    Expected Tools:  {json.dumps(exp_tools)}", file=dbg)
            print(f"    Executed Tools:  {json.dumps(actual_tools)}", file=dbg)
            if actual.get("error"):
                print(f"    Execution Error: {actual.get('error')}", file=dbg)
            print(f"  ── Calculated Turn Metrics ──", file=dbg)
            print(f"    Response Score:  {r_score:.4f}", file=dbg)
            print(f"    Tool Use Score:  {t_score:.4f}", file=dbg)
            print(f"    Groundedness:    {g_score:.4f}", file=dbg)
            print(f"    Safety Check:    {s_score:.4f}", file=dbg)
            print("-" * 60, file=dbg)

        print("\n" + "=" * 60, file=dbg)
        print(" END OF TESTING STEP COMPARISONS ", file=dbg)
        print("=" * 60 + "\n", file=dbg)

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


# ── SCORING HELPERS ───────────────────────────────────────────────────────────

def _score_tool_calls(
    expected: List[Dict],
    actual: List[Dict],
    name_only: bool = False,
) -> float:
    if not expected:
        return 1.0
    if not actual:
        return 0.0

    actual_by_name: Dict[str, List[Dict]] = {}
    for at in actual:
        actual_by_name.setdefault(at["tool_name"], []).append(at)

    score = 0.0
    for et in expected:
        candidates = actual_by_name.get(et["tool_name"], [])
        if not candidates:
            continue

        if name_only:
            score += 1.0
            continue

        ep = et.get("parameters", {})
        if not ep:
            score += 1.0
            continue

        full_match = any(
            all(c.get("parameters", {}).get(k) == v for k, v in ep.items())
            for c in candidates
        )
        score += 1.0 if full_match else 0.5

    return min(score / len(expected), 1.0)


def _score_response(expected_response: str, actual: str, pattern: Dict) -> float:
    if not actual:
        return 0.0

    actual_clean   = actual.lower()
    expected_clean = expected_response.lower() if expected_response else ""

    for bad in pattern.get("must_not_contain", []):
        if bad.lower() in actual_clean:
            return 0.0

    if not expected_clean:
        return 0.8 if len(actual_clean) > 50 else 0.5

    expected_words = [
        w.strip(".,!?|:()[]#*-")
        for w in expected_clean.split()
        if len(w.strip(".,!?|:()[]#*-")) > 3
    ]
    if not expected_words:
        return 0.8 if len(actual_clean) > 50 else 0.5

    matched = sum(1 for w in expected_words if w in actual_clean)
    return matched / len(expected_words)


def _score_safety(actual: str) -> float:
    if not actual:
        return 1.0
    clean = actual.lower()
    for word in EvaluationConfig.UNSAFE_KEYWORDS:
        if word in clean:
            return 0.0
    return 1.0


def _score_groundedness(actual: str) -> float:
    if not actual:
        return 0.0

    clean        = actual
    lower_clean  = clean.lower()

    struct_signals = [
        bool(re.search(r'\|.+\|', clean)),                         
        bool(re.search(r'\*{1,2}[^*\n]{2,}\*{1,2}', clean)),      
        bool(re.search(r'(?i)(filters?|segment|breakdown)\s*:', clean)),  
        bool(re.search(r'\d[\d,]+', clean)),                       
        bool(re.search(r'^#{1,3}\s+\S', clean, re.MULTILINE)),    
    ]
    structural_score = sum(struct_signals) / len(struct_signals)

    length_score = 0.0 if len(clean) < 30 else min(len(clean) / 300, 1.0)

    domain_words = [
        "finance", "segment", "account", "contact", "audience",
        "campaign", "filter", "export", "job level", "job function",
        "executive", "c-suite", "tax", "accounting", "refined",
        "breakdown", "total", "country",
    ]
    hits = sum(1 for w in domain_words if w in lower_clean)
    coherence_score = min(hits / 4, 1.0)   

    return round(
        0.40 * structural_score
        + 0.40 * length_score
        + 0.20 * coherence_score,
        4,
    )


def _zero_metrics() -> Dict[str, Any]:
    return {k: 0.0 for k in EVAL_CRITERIA.keys()}


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
    n   = total_turns or 1
    sr  = successful_turns           / n
    at  = sum(tool_scores)           / n
    ar  = sum(response_scores)       / n
    ag  = sum(groundedness_scores)   / n
    as_ = sum(safety_scores)         / n
    tu  = perfect_tool_turns         / n

    computed = {
        "session_id":                        session_id,
        "tool_trajectory_avg_score":          at,
        "response_match_score":              ar,
        "groundedness_v1":                    ag,
        "safety_v1":                         as_,
        "multi_turn_task_success_v1":        sr,
        "multi_turn_trajectory_quality_v1":  (sr + at + ar) / 3.0,
        "multi_turn_tool_use_quality_v1":    tu,
        "final_response_match_v2":           response_scores[-1] if response_scores else 0.0,
    }
    
    return {k: computed.get(k, 0.0) for k in ["session_id"] + list(EVAL_CRITERIA.keys())}


# ── BIGQUERY ──────────────────────────────────────────────────────────────────

def calculate_aggregate_metrics(all_metrics: List[Dict]) -> Dict[str, Any]:
    if not all_metrics:
        return {}

    agg: Dict[str, Any] = {
        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_endpoint":      RESOURCE_NAME,
        "total_test_cases":    len(all_metrics),
    }

    for k in EVAL_CRITERIA.keys():
        vals   = [m[k] for m in all_metrics if k in m]
        agg[k] = sum(vals) / len(vals) if vals else 0.0

    agg["overall_status"] = (
        "PASSED"
        if all(agg.get(k, 0.0) >= threshold for k, threshold in EVAL_CRITERIA.items())
        else "FAILED"
    )
    return agg


def _format_score_for_bq(value: float, metric_key: str) -> str:
    threshold = EVAL_CRITERIA.get(metric_key, 0.0)
    status    = "PASS" if value >= threshold else "FAIL"
    pct       = int(round(value * 100))
    return f"{status}({pct}%)"


def record_snapshot(agg_metrics: Dict[str, Any], all_session_metrics: List[Dict]) -> None:
    table_ref = (
        f"{EvaluationConfig.PROJECT_ID}."
        f"{EvaluationConfig.BQ_DATASET}."
        f"{EvaluationConfig.BQ_TABLE}"
    )

    all_bq_columns = EvaluationConfig.get_all_bq_columns()
    rows: List[Dict[str, Any]] = []

    for m in all_session_metrics:
        row: Dict[str, Any] = {}
        
        for col in all_bq_columns:
            if col == "execution_timestamp":
                row[col] = str(agg_metrics.get("execution_timestamp", ""))
            elif col == "session_id":
                row[col] = str(m.get("session_id", "unknown"))
            elif col == "agent_endpoint":
                row[col] = str(agg_metrics.get("agent_endpoint", ""))
            elif col == "overall_status":
                row[col] = str(agg_metrics.get("overall_status", "FAILED"))
            else:
                value = float(m.get(col, 0.0))
                row[col] = _format_score_for_bq(value, col)

        rows.append(row)

    errors = bq_client.insert_rows_json(table_ref, rows)
    if errors:
        print("BigQuery insert errors occurred:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def run_pipeline() -> None:
    global EVAL_CRITERIA

    try:
        EVAL_CRITERIA = EvaluationConfig.load_eval_criteria()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        print(f"Configuration Error: {e}", file=sys.stderr)
        return

    golden_data = load_golden_dataset()
    if not golden_data or not golden_data.get("test_cases"):
        print("Error: No test cases found in the golden dataset.", file=sys.stderr)
        return

    golden_test_case = golden_data["test_cases"][0]

    session_id = input("Enter a session ID to evaluate an existing session, or press Enter to run a live session: ").strip()

    if not initialize_agent():
        print("Error: Could not initialize the agent client.", file=sys.stderr)
        return

    if session_id:
        metrics = evaluate_session_id(session_id, golden_test_case)
        actual_session = session_id
    else:
        print("Running live evaluation, this may take a moment...")
        metrics = evaluate_live_session(golden_test_case)
        actual_session = metrics.get("session_id", "N/A")

    agg = calculate_aggregate_metrics([metrics])
    record_snapshot(agg, [metrics])

    print(f"\nEvaluation completed for session: {actual_session}")
    print(f"Detailed turn-by-turn evaluation written to: evaluation_debug.txt")


if __name__ == "__main__":
    run_pipeline()
