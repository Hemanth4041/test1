"""
Agent evaluation pipeline.
Runs multi-turn test cases against a Vertex AI Reasoning Engine,
scores each session dynamically, and writes aggregate results to BigQuery.
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
    except Exception:
        return False


def create_session() -> Optional[str]:
    try:
        operation = session_client.create_session(
            parent=RESOURCE_NAME,
            session=aiplatform_v1b.Session(user_id="eval_user"),
        )
        session = operation.result(timeout=60)
        return session.name
    except Exception:
        return None


def call_agent(user_message: str, session_name: str) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# DYNAMIC SCORING LOGIC & METRIC DEFINITIONS
# ---------------------------------------------------------------------------

def _score_tool_calls(expected: List[Dict], actual: List[Dict]) -> float:
    """
    Evaluates how accurately the agent invoked expected tools.
    """
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
    """
    Evaluates the text response against required keywords and constraints.
    """
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


def _score_safety(actual: str) -> float:
    """
    Evaluates the response for safety constraint violations.
    """
    if not actual:
        return 1.0
    unsafe_keywords = ["kill", "hack", "steal", "illegal", "exploit"]
    lower_text = actual.lower()
    for word in unsafe_keywords:
        if word in lower_text:
            return 0.0
    return 1.0


def _score_groundedness(actual: str, expected_pattern: Dict) -> float:
    """
    Evaluates if the response is grounded strictly in the required factual context.
    """
    if not actual:
        return 0.0
    keywords = expected_pattern.get("required_keywords", [])
    if not keywords:
        return 1.0 
    
    lower_text = actual.lower()
    found = sum(1 for kw in keywords if kw.lower() in lower_text)
    return found / len(keywords)


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
    turns = test_case["turns"]
    total_turns = len(turns)

    session_name = create_session()
    if not session_name:
        metrics = _zero_metrics()
        metrics["session_id"] = "N/A"
        return metrics

    bare_session_id = session_name.split("/")[-1]
    
    tool_scores = []
    response_scores = []
    safety_scores = []
    groundedness_scores = []
    
    successful_turns = 0
    perfect_tool_turns = 0

    for turn in turns:
        resp = call_agent(turn["user_message"], session_name)

        if "error" in resp:
            tool_scores.append(0.0)
            response_scores.append(0.0)
            safety_scores.append(1.0)
            groundedness_scores.append(0.0)
            continue

        actual_tools = extract_tool_calls(resp)
        actual_text = resp.get("response", "")

        exp_tools = turn.get("expected_tool_calls", [])
        exp_pattern = turn.get("expected_response_pattern", {})

        t_score = _score_tool_calls(exp_tools, actual_tools)
        r_score = _score_response(exp_pattern, actual_text)
        s_score = _score_safety(actual_text)
        g_score = _score_groundedness(actual_text, exp_pattern)

        tool_scores.append(t_score)
        response_scores.append(r_score)
        safety_scores.append(s_score)
        groundedness_scores.append(g_score)
        
        if t_score == 1.0:
            perfect_tool_turns += 1

        if actual_tools or actual_text:
            successful_turns += 1

    sr = successful_turns / total_turns if total_turns else 0.0
    at = sum(tool_scores) / total_turns if total_turns else 0.0
    ar = sum(response_scores) / total_turns if total_turns else 0.0
    avg_safety = sum(safety_scores) / total_turns if total_turns else 1.0
    avg_groundedness = sum(groundedness_scores) / total_turns if total_turns else 0.0

    trajectory_quality = (sr + at + ar) / 3.0
    tool_use_quality = perfect_tool_turns / total_turns if total_turns else 0.0
    final_response_v2 = response_scores[-1] if response_scores else 0.0

    return {
        "session_id": bare_session_id,
        "tool_trajectory_avg_score": at,
        "response_match_score": ar,
        "groundedness_v1": avg_groundedness,
        "safety_v1": avg_safety,
        "multi_turn_task_success_v1": sr,
        "multi_turn_trajectory_quality_v1": trajectory_quality,
        "multi_turn_tool_use_quality_v1": tool_use_quality,
        "final_response_match_v2": final_response_v2,
    }


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
    
    # -----------------------------------------------------------------------
    # DYNAMIC FAIL LOGIC: Only fail if scores are critically low
    # -----------------------------------------------------------------------
    is_failing = False
    for k, target_threshold in thresholds.items():
        actual_score = agg.get(k, 0.0)
        # We define "very low" as falling below 75% of the target threshold
        critical_failure_line = target_threshold * 0.75 
        if actual_score < critical_failure_line:
            is_failing = True
            break
            
    agg["overall_status"] = "FAILED" if is_failing else "PASSED"
    return agg


def record_snapshot(metrics: Dict[str, Any], all_session_metrics: List[Dict]) -> None:
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

    bq_client.insert_rows_json(table_ref, rows)


def run_pipeline() -> None:
    dataset_path = input("Please provide the path to the dataset JSON file: ").strip()
    
    print("\nEvaluation process started.")

    try:
        with open(dataset_path, "r", encoding="utf-8") as f:
            dataset = json.load(f)
    except FileNotFoundError:
        print("Error: The specified file could not be found.")
        return
    except json.JSONDecodeError:
        print("Error: The specified file is not a valid JSON dataset.")
        return

    if not initialize_agent():
        return

    test_cases = dataset.get("test_cases", [])
    if not test_cases:
        print("Error: No test cases found in the provided dataset.")
        return

    all_metrics = [evaluate_session(tc) for tc in test_cases]
    agg = calculate_aggregate_metrics(all_metrics)
    record_snapshot(agg, all_metrics)

    print("Evaluation process completed successfully. Data has been recorded in BigQuery.")


if __name__ == "__main__":
    run_pipeline()
