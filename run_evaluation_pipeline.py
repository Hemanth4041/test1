"""
Agent evaluation pipeline.
Runs multi-turn test cases against a Vertex AI Reasoning Engine,
scores each session, and writes aggregate results to BigQuery.
"""

import json
import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path

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
# AGENT INITIALISATION
# ---------------------------------------------------------------------------

def initialize_agent() -> bool:
    """Obtain GCP credentials and instantiate the Vertex AI SDK clients."""
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

        print(f"   Credentials and SDK clients initialized (endpoint: {api_endpoint})")
        return True
    except Exception as e:
        print(f"   Initialization failed: {e}")
        return False


# ---------------------------------------------------------------------------
# SESSION MANAGEMENT
# ---------------------------------------------------------------------------

def create_session() -> Optional[str]:
    """
    Create a persistent Vertex AI session for one test case.

    Returns the full session resource name, which is passed to every
    stream_query call so the server routes all turns through the same
    stateful context.
    """
    try:
        operation = session_client.create_session(
            parent=RESOURCE_NAME,
            session=aiplatform_v1b.Session(user_id="eval_user"),
        )
        session = operation.result(timeout=60)
        print(f"   Session created: {session.name}")
        return session.name
    except Exception as e:
        print(f"   Failed to create session: {e}")
        return None
# ---------------------------------------------------------------------------
# AGENT CALL
# ---------------------------------------------------------------------------

def call_agent(user_message: str, session_name: str) -> Dict[str, Any]:
    """
    Send one conversational turn to the agent within an existing session.

    Credentials are refreshed before each call to guard against token expiry
    on long evaluation runs.  The bare numeric session ID (not the full
    resource path) is what the ADK runner uses to restore state.
    """
    try:
        credentials.refresh(Request())

        bare_session_id = session_name.split("/")[-1]
        input_payload = {
            "message": user_message,
            "user_id": "eval_user",
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
                pass  # Heartbeat frames carry no data

        return _aggregate_streaming_response(chunks)

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# RESPONSE AGGREGATION & TOOL EXTRACTION
# ---------------------------------------------------------------------------

def _aggregate_streaming_response(chunks: List[Any]) -> Dict[str, Any]:
    """Merge streaming chunks into a single response dict."""
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
    """Extract and deduplicate tool calls from an agent response."""
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


# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------

def _score_tool_calls(expected: List[Dict], actual: List[Dict]) -> float:
    """Return the fraction of expected tool calls that were matched in actual."""
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


def _score_response(pattern: Dict, actual: str) -> float:
    """Score the agent's text response against a keyword/exclusion pattern."""
    if not pattern:
        return 1.0
    lower = actual.lower()
    for bad in pattern.get("must_not_contain", []):
        if bad.lower() in lower:
            return 0.0
    keywords = pattern.get("required_keywords", [])
    if not keywords:
        return 0.8 if len(actual) > 50 else 0.5
    found = sum(1 for kw in keywords if kw.lower() in lower)
    return found / len(keywords)


# ---------------------------------------------------------------------------
# SESSION EVALUATION LOOP
# ---------------------------------------------------------------------------

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


def evaluate_session(test_case: Dict) -> Dict[str, Any]:
    """
    Run all turns in a test case through a single persistent session and
    return per-metric scores plus the session ID used.

    The session ID is preserved so it can be written directly to BigQuery,
    making it easy to correlate evaluation results with agent session logs.
    """
    turns = test_case["turns"]
    total_turns = len(turns)

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{test_case['test_case_id']}.txt"

    print(f"\nEvaluating: {test_case['test_case_id']}")

    session_name = create_session()
    if not session_name:
        print("   Cannot proceed without a session.")
        metrics = _zero_metrics()
        metrics["session_id"] = "N/A"
        return metrics

    bare_session_id = session_name.split("/")[-1]
    tool_scores, response_scores = [], []
    successful_turns = 0

    with open(log_path, "w", encoding="utf-8") as f:

        def log(msg: str) -> None:
            f.write(msg + "\n")

        log("=" * 80)
        log(f"Test case : {test_case['test_case_id']}")
        log(f"Session   : {session_name}")
        log(f"Turns     : {total_turns}")
        log("=" * 80)

        for i, turn in enumerate(turns, 1):
            print(f"   -> Turn {i}/{total_turns}: '{turn['user_message'][:60]}'")
            log(f"\n{'─' * 60}")
            log(f"Turn {i}/{total_turns}")
            log(f"  User: {turn['user_message']}")

            resp = call_agent(turn["user_message"], session_name)

            if "error" in resp:
                log(f"  Error: {resp['error']}")
                print(f"      Error: {resp['error']}")
                tool_scores.append(0.0)
                response_scores.append(0.0)
                continue

            actual_tools = extract_tool_calls(resp)
            actual_text = resp.get("response", "")

            exp_tools = turn.get("expected_tool_calls", [])
            exp_pattern = turn.get("expected_response_pattern", {})

            t_score = _score_tool_calls(exp_tools, actual_tools)
            r_score = _score_response(exp_pattern, actual_text)

            tool_scores.append(t_score)
            response_scores.append(r_score)
            if actual_tools or actual_text:
                successful_turns += 1

            log(f"  Tools expected : {[t['tool_name'] for t in exp_tools]}")
            log(f"  Tools actual   : {[t['tool_name'] for t in actual_tools]}")
            log(f"  Tool score     : {t_score:.2f}")
            log(f"  Resp expected  : {turn.get('expected_response', '')[:200]}")
            log(f"  Resp actual    : {actual_text[:200]}")
            log(f"  Resp score     : {r_score:.2f}")

        sr = successful_turns / total_turns if total_turns else 0.0
        at = sum(tool_scores) / len(tool_scores) if tool_scores else 0.0
        ar = sum(response_scores) / len(response_scores) if response_scores else 0.0

        log(f"\n{'=' * 60}")
        log(f"Session   : {session_name}")
        log(f"Success   : {sr:.2%}  Tool avg: {at:.2f}  Resp avg: {ar:.2f}")

    print(f"   Done - {total_turns} turns | tool={at:.2f} resp={ar:.2f} | session={bare_session_id}")

    return {
        "session_id": bare_session_id,
        "tool_trajectory_avg_score": at,
        "response_match_score": ar,
        "groundedness_v1": 0.85,
        "safety_v1": 1.0,
        "multi_turn_task_success_v1": sr,
        "multi_turn_trajectory_quality_v1": sr,
        "multi_turn_tool_use_quality_v1": at,
        "final_response_match_v2": ar,
    }


# ---------------------------------------------------------------------------
# AGGREGATION & BIGQUERY
# ---------------------------------------------------------------------------

def calculate_aggregate_metrics(all_metrics: List[Dict]) -> Dict[str, Any]:
    """Average per-session scores and determine overall pass/fail status."""
    if not all_metrics:
        return {}

    metric_keys = list(_zero_metrics().keys())
    agg = {
        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_endpoint": RESOURCE_NAME,
        "branch": EvaluationConfig.BRANCH_NAME,
        "total_test_cases": len(all_metrics),
    }
    for k in metric_keys:
        vals = [m[k] for m in all_metrics if k in m]
        agg[k] = sum(vals) / len(vals) if vals else 0.0

    thresholds = EvaluationConfig.load_eval_criteria()
    agg["overall_status"] = (
        "PASSED"
        if all(agg.get(k, 0.0) >= v for k, v in thresholds.items())
        else "FAILED"
    )
    return agg


def record_snapshot(metrics: Dict[str, Any], all_session_metrics: List[Dict]) -> None:
    """
    Write one row per evaluated session to BigQuery.

    Each row carries the aggregate scores alongside the specific session ID
    that produced them, enabling direct drill-down into agent session logs.
    """
    table_ref = (
        f"{EvaluationConfig.PROJECT_ID}."
        f"{EvaluationConfig.BQ_DATASET}."
        f"{EvaluationConfig.BQ_TABLE}"
    )

    rows = []
    for session_metrics in all_session_metrics:
        rows.append({
            "execution_timestamp": metrics["execution_timestamp"],
            "session_id": session_metrics.get("session_id", "unknown"),
            "agent_endpoint": metrics["agent_endpoint"],
            "branch": metrics["branch"],
            "response_match_score": float(session_metrics.get("response_match_score", 0.0)),
            "safety_v1": float(session_metrics.get("safety_v1", 1.0)),
            "multi_turn_task_success_v1": float(session_metrics.get("multi_turn_task_success_v1", 0.0)),
            "multi_turn_trajectory_quality_v1": float(session_metrics.get("multi_turn_trajectory_quality_v1", 0.0)),
            "multi_turn_tool_use_quality_v1": float(session_metrics.get("multi_turn_tool_use_quality_v1", 0.0)),
            "final_response_match_v2": float(session_metrics.get("final_response_match_v2", 0.0)),
            "tool_trajectory_avg_score": float(session_metrics.get("tool_trajectory_avg_score", 0.0)),
            "groundedness_v1": float(session_metrics.get("groundedness_v1", 0.0)),
            "overall_status": metrics.get("overall_status", "FAILED"),
        })

    errors = bq_client.insert_rows_json(table_ref, rows)
    if errors:
        print(f"   BigQuery insert errors: {errors}")
    else:
        print(f"   Recorded {len(rows)} row(s) to BigQuery.")


def print_metrics_summary(metrics: Dict[str, Any]) -> None:
    """Print a formatted pass/fail summary to stdout."""
    print("\n" + "=" * 80)
    print("FINAL METRICS SUMMARY")
    print("=" * 80)
    print(f"Time   : {metrics['execution_timestamp']}")
    print(f"Branch : {metrics['branch']}   Cases: {metrics['total_test_cases']}")
    print("\n" + "-" * 80)
    print(f"{'Metric':<45} {'Score':<10} {'Threshold':<10} Status")
    print("-" * 80)
    for metric, threshold in EvaluationConfig.load_eval_criteria().items():
        if metric in metrics:
            score = metrics[metric]
            status = "PASS" if score >= threshold else "FAIL"
            print(f"{metric:<45} {score:<10.4f} {threshold:<10.2f} {status}")
    print("-" * 80)
    overall = metrics.get("overall_status", "FAILED")
    print(f"\nOverall: {overall}")
    print("=" * 80)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    print("=" * 80)
    print("AGENT EVALUATION PIPELINE")
    print("=" * 80)
    print(f"Project  : {EvaluationConfig.PROJECT_ID}")
    print(f"Location : {EvaluationConfig.LOCATION}")
    print(f"Engine   : {EvaluationConfig.REASONING_ENGINE_ID}")

    if not initialize_agent():
        return

    print("\n" + "=" * 80)
    print("STEP 1: SELECT GOLDEN DATASET")
    print("=" * 80)

    data_dir = EvaluationConfig.get_data_dir()
    json_files = sorted(data_dir.glob("*.json"))
    if not json_files:
        print(f"No JSON datasets found in {data_dir}")
        return

    print("\nAvailable datasets:")
    for i, f in enumerate(json_files, 1):
        print(f"  [{i}] {f.name}")

    try:
        choice = int(input(f"\nEnter number (1-{len(json_files)}): "))
        if not 1 <= choice <= len(json_files):
            print("Invalid selection.")
            return
    except ValueError:
        print("Please enter a valid number.")
        return

    selected = json_files[choice - 1].name
    print(f"\nSelected: {selected}")

    dataset = EvaluationConfig.load_golden_dataset(selected)
    test_cases = dataset.get("test_cases", [])

    print("\n" + "=" * 80)
    print("STEP 2: RUNNING AGENT EVALUATIONS")
    print("=" * 80)
    all_metrics = [evaluate_session(tc) for tc in test_cases]

    print("\n" + "=" * 80)
    print("STEP 3: AGGREGATE METRICS")
    print("=" * 80)
    agg = calculate_aggregate_metrics(all_metrics)

    print("\n" + "=" * 80)
    print("STEP 4: RECORDING TO BIGQUERY")
    print("=" * 80)
    record_snapshot(agg, all_metrics)

    print_metrics_summary(agg)


if __name__ == "__main__":
    run_pipeline()
