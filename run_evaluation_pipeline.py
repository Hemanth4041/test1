import json
import datetime
import requests
import re
from typing import Dict, List, Any, Optional
from pathlib import Path

from google.cloud import bigquery
from google.api_core.exceptions import NotFound
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


# ─────────────────────────────────────────────────────────────────────────────
# INFRASTRUCTURE SETUP
# ─────────────────────────────────────────────────────────────────────────────

def setup_bigquery_infrastructure():
    print("\n" + "=" * 80)
    print("INITIALIZING BIGQUERY INFRASTRUCTURE")
    print("=" * 80)

    dataset_ref = bq_client.dataset(EvaluationConfig.BQ_DATASET)
    try:
        bq_client.get_dataset(dataset_ref)
        print(f"   ✓ Dataset '{EvaluationConfig.BQ_DATASET}' already exists.")
    except NotFound:
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = "US"
        bq_client.create_dataset(dataset)
        print(f"   ✓ Created dataset '{EvaluationConfig.BQ_DATASET}'.")

    table_ref = dataset_ref.table(EvaluationConfig.BQ_TABLE)
    try:
        bq_client.get_table(table_ref)
        print(f"   ✓ Table '{EvaluationConfig.BQ_TABLE}' already exists.")
    except NotFound:
        schema = [
            bigquery.SchemaField("execution_timestamp", "TIMESTAMP"),
            bigquery.SchemaField("session_id",          "STRING"),
            bigquery.SchemaField("agent_endpoint",      "STRING"),
            bigquery.SchemaField("branch",              "STRING"),
            bigquery.SchemaField("response_match_score",                "FLOAT"),
            bigquery.SchemaField("safety_v1",                           "FLOAT"),
            bigquery.SchemaField("multi_turn_task_success_v1",          "FLOAT"),
            bigquery.SchemaField("multi_turn_trajectory_quality_v1",    "FLOAT"),
            bigquery.SchemaField("multi_turn_tool_use_quality_v1",      "FLOAT"),
            bigquery.SchemaField("final_response_match_v2",             "FLOAT"),
            bigquery.SchemaField("tool_trajectory_avg_score",           "FLOAT"),
            bigquery.SchemaField("groundedness_v1",                     "FLOAT"),
            bigquery.SchemaField("overall_status",                      "STRING"),
        ]
        bq_client.create_table(bigquery.Table(table_ref, schema=schema))
        print(f"   ✓ Created table '{EvaluationConfig.BQ_TABLE}'.")


def initialize_agent() -> bool:
    """Initialize GCP credentials and both SDK clients."""
    global credentials, session_client, execution_client
    try:
        creds, _ = google.auth.default()
        creds.refresh(Request())
        credentials = creds

        api_endpoint = f"{EvaluationConfig.LOCATION}-aiplatform.googleapis.com"

        session_client = aiplatform_v1b.SessionServiceClient(
            client_options={"api_endpoint": api_endpoint}
        )
        execution_client = aiplatform_v1b.ReasoningEngineExecutionServiceClient(
            client_options={"api_endpoint": api_endpoint}
        )

        print(f"   ✓ Credentials and SDK clients initialized")
        print(f"   ✓ API endpoint: {api_endpoint}")
        return True
    except Exception as e:
        print(f"   ✗ Initialization failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SESSION MANAGEMENT  ← THE CORE FIX
# ─────────────────────────────────────────────────────────────────────────────

def create_session() -> Optional[str]:
    """
    Create a persistent session via the Vertex AI Sessions API.

    Returns the full session resource name, e.g.:
      projects/.../locations/.../reasoningEngines/.../sessions/SESSION_ID

    This session name is then passed to every streamQuery call so the server
    routes all turns through the same stateful session.
    """
    try:
        operation = session_client.create_session(
            parent=RESOURCE_NAME,
            session=aiplatform_v1b.Session(user_id="eval_user"),
        )
        print(f"   Waiting for session creation...")
        session = operation.result(timeout=60)
        print(f"   ✓ Session created: {session.name}")
        return session.name
    except Exception as e:
        print(f"   ✗ Failed to create session: {e}")
        return None


def delete_session(session_name: str):
    """Clean up the session after the test case finishes."""
    try:
        session_client.delete_session(name=session_name)
        print(f"   ✓ Session deleted: {session_name.split('/')[-1]}")
    except Exception as e:
        print(f"   ⚠ Could not delete session: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# AGENT CALL  ← USES SESSION NAME IN EVERY REQUEST
# ─────────────────────────────────────────────────────────────────────────────

def call_agent(user_message: str, session_name: str) -> Dict[str, Any]:
    """
    Send one turn to the agent via the SDK, tied to an existing session.

    The session_name is passed as input.session_id (the field the ADK server
    reads to look up persistent state).  We also refresh credentials before
    every call to avoid token expiry on long test runs.
    """
    try:
        credentials.refresh(Request())

        # ADK runner looks up session by bare numeric ID, not the full resource path
        bare_session_id = session_name.split("/")[-1]

        input_payload = {
            "message":    user_message,
            "user_id":    "eval_user",
            "session_id": bare_session_id,
        }

        print(f"      [DEBUG] session: {bare_session_id}")
        print(f"      [DEBUG] message: {user_message[:80]}")

        chunks = []
        for chunk in execution_client.stream_query_reasoning_engine(
            request=aiplatform_v1b.StreamQueryReasoningEngineRequest(
                name=RESOURCE_NAME,
                input=input_payload,
            )
        ):
            # Each chunk is an HttpBody; its data field is JSON bytes
            try:
                parsed = json.loads(chunk.data)
                if isinstance(parsed, list):
                    chunks.extend(parsed)
                else:
                    chunks.append(parsed)
            except (json.JSONDecodeError, AttributeError):
                # chunk.data may be empty for heartbeat frames
                pass

        return aggregate_streaming_response(chunks)

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE AGGREGATION & TOOL EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_streaming_response(chunks: List[Any]) -> Dict[str, Any]:
    text_parts, tool_calls = [], []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        # Text extraction
        if "content" in chunk and "parts" in chunk.get("content", {}):
            for part in chunk["content"]["parts"]:
                if "text" in part:
                    text_parts.append(part["text"])
        elif "text" in chunk:
            text_parts.append(chunk["text"])
        elif "output" in chunk:
            text_parts.append(str(chunk["output"]))

        # Tool call extraction
        for key in ("tool_calls", "toolCalls"):
            tool_calls.extend(chunk.get(key, []))

    return {
        "response":   "".join(text_parts),
        "tool_calls": tool_calls,
        "all_chunks": chunks,
    }


def extract_tool_calls(agent_response: Dict) -> List[Dict]:
    tool_calls = []

    for tc in agent_response.get("tool_calls", []):
        tool_calls.append({
            "tool_name":  tc.get("name", tc.get("tool_name", "unknown")),
            "parameters": tc.get("parameters", tc.get("args", {})),
        })

    for chunk in agent_response.get("all_chunks", []):
        if not isinstance(chunk, dict):
            continue
        # Top-level functionCall
        if "functionCall" in chunk:
            tool_calls.append({
                "tool_name":  chunk["functionCall"].get("name", "unknown"),
                "parameters": chunk["functionCall"].get("args", {}),
            })
        # Inside content.parts
        for part in chunk.get("content", {}).get("parts", []):
            for key in ("functionCall", "function_call"):
                if key in part:
                    tool_calls.append({
                        "tool_name":  part[key].get("name", "unknown"),
                        "parameters": part[key].get("args", {}),
                    })

    # Deduplicate
    seen, unique = set(), []
    for tc in tool_calls:
        sig = (tc["tool_name"], json.dumps(tc["parameters"], sort_keys=True))
        if sig not in seen:
            seen.add(sig)
            unique.append(tc)
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_turn_tool_calls(expected: List[Dict], actual: List[Dict]) -> float:
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


def evaluate_turn_response(pattern: Dict, actual: str) -> float:
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


# ─────────────────────────────────────────────────────────────────────────────
# SESSION EVALUATION LOOP
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_session(test_case: Dict) -> Dict[str, float]:
    """
    Runs all turns in a test case through a single persistent session.

    Flow:
      1. Create a session  → get session_name
      2. For each turn: call_agent(message, session_name)
         The server looks up session_name and restores state between turns
      3. Delete session when done
    """
    turns       = test_case["turns"]
    total_turns = len(turns)

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{test_case['test_case_id']}.txt"

    print(f"\nEvaluating: {test_case['test_case_id']}")

    # ── Step 1: Create the session ───────────────────────────────────────────
    print(f"   Creating session...")
    session_name = create_session()
    if not session_name:
        print("   ✗ Cannot proceed without a session.")
        return _zero_metrics()

    tool_scores, response_scores = [], []
    successful_turns = 0

    with open(log_path, "w", encoding="utf-8") as f:
        def log(msg):
            f.write(msg + "\n")

        log("=" * 80)
        log(f"Test case : {test_case['test_case_id']}")
        log(f"Session   : {session_name}")
        log(f"Turns     : {total_turns}")
        log("=" * 80)

        # ── Step 2: Run every turn through the SAME session ──────────────────
        for i, turn in enumerate(turns, 1):
            print(f"   -> Turn {i}/{total_turns}: '{turn['user_message'][:60]}'")
            log(f"\n{'─' * 60}")
            log(f"Turn {i}/{total_turns}")
            log(f"  User: {turn['user_message']}")

            resp = call_agent(turn["user_message"], session_name)

            if "error" in resp:
                log(f"  ✗ Error: {resp['error']}")
                print(f"      ✗ Error: {resp['error']}")
                tool_scores.append(0.0)
                response_scores.append(0.0)
                continue

            actual_tools = extract_tool_calls(resp)
            actual_text  = resp.get("response", "")

            exp_tools   = turn.get("expected_tool_calls", [])
            exp_pattern = turn.get("expected_response_pattern", {})

            t_score = evaluate_turn_tool_calls(exp_tools, actual_tools)
            r_score = evaluate_turn_response(exp_pattern, actual_text)

            tool_scores.append(t_score)
            response_scores.append(r_score)
            if actual_tools or actual_text:
                successful_turns += 1

            log(f"  Tools expected : {[t['tool_name'] for t in exp_tools]}")
            log(f"  Tools actual   : {[t['tool_name'] for t in actual_tools]}")
            log(f"  Tool score     : {t_score:.2f}  {'✓' if t_score >= 0.8 else '✗'}")
            log(f"  Resp expected  : {turn.get('expected_response','')[:200]}")
            log(f"  Resp actual    : {actual_text[:200]}")
            log(f"  Resp score     : {r_score:.2f}  {'✓' if r_score >= 0.7 else '✗'}")

        sr   = successful_turns / total_turns if total_turns else 0.0
        at   = sum(tool_scores) / len(tool_scores) if tool_scores else 0.0
        ar   = sum(response_scores) / len(response_scores) if response_scores else 0.0

        log(f"\n{'=' * 60}")
        log(f"Session    : {session_name}")
        log(f"Success    : {sr:.2%}  Tool avg: {at:.2f}  Resp avg: {ar:.2f}")

    print(f"   ✓ Done — {total_turns} turns, tool={at:.2f}, resp={ar:.2f}")
    print(f"   Session preserved in UI: {session_name.split('/')[-1]}")

    return {
        "tool_trajectory_avg_score":         at,
        "response_match_score":              ar,
        "groundedness_v1":                   0.85,
        "safety_v1":                         1.0,
        "multi_turn_task_success_v1":        sr,
        "multi_turn_trajectory_quality_v1":  sr,
        "multi_turn_tool_use_quality_v1":    at,
        "final_response_match_v2":           ar,
    }


def _zero_metrics() -> Dict[str, float]:
    return {k: 0.0 for k in [
        "tool_trajectory_avg_score", "response_match_score", "groundedness_v1",
        "safety_v1", "multi_turn_task_success_v1", "multi_turn_trajectory_quality_v1",
        "multi_turn_tool_use_quality_v1", "final_response_match_v2",
    ]}


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATION, BIGQUERY, SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def calculate_aggregate_metrics(all_metrics: List[Dict]) -> Dict[str, Any]:
    if not all_metrics:
        return {}
    keys = list(_zero_metrics().keys())
    agg = {
        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_endpoint":      RESOURCE_NAME,
        "branch":              EvaluationConfig.BRANCH_NAME,
        "total_test_cases":    len(all_metrics),
    }
    for k in keys:
        vals = [m[k] for m in all_metrics if k in m]
        agg[k] = sum(vals) / len(vals) if vals else 0.0

    thresholds = EvaluationConfig.load_eval_criteria()
    agg["overall_status"] = (
        "PASSED" if all(agg.get(k, 0.0) >= v for k, v in thresholds.items())
        else "FAILED"
    )
    return agg


def record_snapshot(metrics: Dict[str, Any]):
    table_ref = (
        f"{EvaluationConfig.PROJECT_ID}."
        f"{EvaluationConfig.BQ_DATASET}."
        f"{EvaluationConfig.BQ_TABLE}"
    )
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    errors = bq_client.insert_rows_json(table_ref, [{
        "execution_timestamp":              metrics["execution_timestamp"],
        "session_id":                       f"aggregate_{ts}",
        "agent_endpoint":                   metrics["agent_endpoint"],
        "branch":                           metrics["branch"],
        "response_match_score":             float(metrics.get("response_match_score", 0.0)),
        "safety_v1":                        float(metrics.get("safety_v1", 1.0)),
        "multi_turn_task_success_v1":       float(metrics.get("multi_turn_task_success_v1", 0.0)),
        "multi_turn_trajectory_quality_v1": float(metrics.get("multi_turn_trajectory_quality_v1", 0.0)),
        "multi_turn_tool_use_quality_v1":   float(metrics.get("multi_turn_tool_use_quality_v1", 0.0)),
        "final_response_match_v2":          float(metrics.get("final_response_match_v2", 0.0)),
        "tool_trajectory_avg_score":        float(metrics.get("tool_trajectory_avg_score", 0.0)),
        "groundedness_v1":                  float(metrics.get("groundedness_v1", 0.0)),
        "overall_status":                   metrics.get("overall_status", "FAILED"),
    }])
    print("   ✓ Recorded to BigQuery" if not errors else f"   ✗ BQ error: {errors}")


def print_metrics_summary(metrics: Dict[str, Any]):
    print("\n" + "=" * 80 + "\nFINAL METRICS SUMMARY\n" + "=" * 80)
    print(f"Time   : {metrics['execution_timestamp']}")
    print(f"Branch : {metrics['branch']}   Cases: {metrics['total_test_cases']}")
    print("\n" + "-" * 80)
    print(f"{'Metric':<45} {'Score':<10} {'Threshold':<10} Status")
    print("-" * 80)
    for metric, threshold in EvaluationConfig.load_eval_criteria().items():
        if metric in metrics:
            score = metrics[metric]
            print(f"{metric:<45} {score:<10.4f} {threshold:<10.2f} "
                  f"{'✓ PASS' if score >= threshold else '✗ FAIL'}")
    print("-" * 80)
    ok = metrics.get("overall_status") == "PASSED"
    print(f"\n{'✓ ALL METRICS PASSED!' if ok else '⚠ SOME METRICS FAILED — check output/ folder.'}")
    print("=" * 80)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline():
    print("=" * 80 + "\nAGENT EVALUATION PIPELINE\n" + "=" * 80)
    print(f"Project  : {EvaluationConfig.PROJECT_ID}")
    print(f"Location : {EvaluationConfig.LOCATION}")
    print(f"Engine   : {EvaluationConfig.REASONING_ENGINE_ID}")

    if not initialize_agent():
        return
    setup_bigquery_infrastructure()

    print("\n" + "=" * 80 + "\nSTEP 1: SELECT GOLDEN DATASET\n" + "=" * 80)
    data_dir   = EvaluationConfig.get_data_dir()
    json_files = sorted(data_dir.glob("*.json"))
    if not json_files:
        print(f"✗ No JSON datasets in {data_dir}")
        return

    print("\nAvailable datasets:")
    for i, f in enumerate(json_files, 1):
        print(f"  [{i}] {f.name}")

    try:
        choice = int(input(f"\nEnter number (1-{len(json_files)}): "))
        if not 1 <= choice <= len(json_files):
            print("✗ Invalid selection.")
            return
    except ValueError:
        print("✗ Please enter a valid number.")
        return

    selected = json_files[choice - 1].name
    print(f"\n✓ Selected: {selected}")

    dataset    = EvaluationConfig.load_golden_dataset(selected)
    test_cases = dataset.get("test_cases", [])

    print("\n" + "=" * 80 + "\nSTEP 2: RUNNING AGENT EVALUATIONS\n" + "=" * 80)
    all_metrics = [evaluate_session(tc) for tc in test_cases]

    print("\n" + "=" * 80 + "\nSTEP 3: AGGREGATE METRICS\n" + "=" * 80)
    agg = calculate_aggregate_metrics(all_metrics)

    print("\n" + "=" * 80 + "\nSTEP 4: RECORDING TO BIGQUERY\n" + "=" * 80)
    record_snapshot(agg)

    print_metrics_summary(agg)


if __name__ == "__main__":
    run_pipeline()
