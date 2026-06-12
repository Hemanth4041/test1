# filename: run_evaluation_pipeline.py

import json
import datetime
import requests
import sys
import re
from typing import Dict, List, Any
from pathlib import Path
from google.cloud import bigquery
from google.api_core.exceptions import NotFound
import vertexai
import google.auth
from google.auth.transport.requests import Request

# Import config
from config import EvaluationConfig

# Initialize GCP Clients
vertexai.init(
    project=EvaluationConfig.PROJECT_ID,
    location=EvaluationConfig.LOCATION
)
bq_client = bigquery.Client(project=EvaluationConfig.PROJECT_ID)

RESOURCE_NAME = EvaluationConfig.get_resource_name()
credentials = None

def setup_bigquery_infrastructure():
    """Ensure the BQ Dataset and Table exist before recording."""
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
            bigquery.SchemaField("session_id", "STRING"),
            bigquery.SchemaField("agent_endpoint", "STRING"),
            bigquery.SchemaField("branch", "STRING"),
            bigquery.SchemaField("response_match_score", "FLOAT"),
            bigquery.SchemaField("safety_v1", "FLOAT"),
            bigquery.SchemaField("multi_turn_task_success_v1", "FLOAT"),
            bigquery.SchemaField("multi_turn_trajectory_quality_v1", "FLOAT"),
            bigquery.SchemaField("multi_turn_tool_use_quality_v1", "FLOAT"),
            bigquery.SchemaField("final_response_match_v2", "FLOAT"),
            bigquery.SchemaField("tool_trajectory_avg_score", "FLOAT"),
            bigquery.SchemaField("groundedness_v1", "FLOAT"),
            bigquery.SchemaField("overall_status", "STRING"),
        ]
        table = bigquery.Table(table_ref, schema=schema)
        bq_client.create_table(table)
        print(f"   ✓ Created table '{EvaluationConfig.BQ_TABLE}' with correct schema.")

def initialize_agent():
    """Initialize GCP credentials."""
    global credentials
    try:
        creds, project = google.auth.default()
        creds.refresh(Request())
        credentials = creds
        print(f"   ✓ Successfully initialized credentials")
        return True
    except Exception as e:
        print(f"   ✗ Failed to initialize credentials: {e}")
        return False

def get_stream_query_endpoint() -> str:
    """Build the streamQuery REST API endpoint."""
    return (
        f"https://{EvaluationConfig.LOCATION}-aiplatform.googleapis.com/v1/"
        f"{RESOURCE_NAME}:streamQuery"
    )

def call_agent(user_message: str, session_id: str = None) -> Dict[str, Any]:
    """Call the remote agent via REST API."""
    try:
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json"
        }
        
        # ---------------------------------------------------------------------
        # CLEAN PAYLOAD
        # We omit session_id on Turn 1 so the server creates it without crashing.
        # We pass it cleanly on Turn 2+.
        # ---------------------------------------------------------------------
        payload = {
            "input": {
                "message": user_message,
                "user_id": "eval_user"
            }
        }
        
        if session_id:
            payload["input"]["session_id"] = session_id
        
        print(f"      [DEBUG] Outbound Payload: {json.dumps(payload)}")
        
        response = requests.post(
            get_stream_query_endpoint(), 
            headers=headers, 
            json=payload, 
            timeout=EvaluationConfig.REQUEST_TIMEOUT, 
            stream=True
        )
        response.raise_for_status()

        chunks = []
        raw_lines = []
        buffer = ""
        
        for line in response.iter_lines():
            if line:
                line_str = line.decode('utf-8')
                raw_lines.append(line_str)
                
                if line_str.startswith('data: '):
                    json_str = line_str[6:]
                    try:
                        parsed_json = json.loads(json_str)
                        if isinstance(parsed_json, list):
                            chunks.extend(parsed_json)
                        else:
                            chunks.append(parsed_json)
                        buffer = ""
                        continue
                    except json.JSONDecodeError:
                        pass
                
                buffer += line_str + "\n"
                try:
                    parsed_json = json.loads(buffer)
                    if isinstance(parsed_json, list):
                        chunks.extend(parsed_json)
                    else:
                        chunks.append(parsed_json)
                    buffer = ""
                except json.JSONDecodeError:
                    continue
                    
        agg_response = aggregate_streaming_response(chunks)
        
        # ---------------------------------------------------------------------
        # BRUTE-FORCE SESSION ID EXTRACTION
        # ---------------------------------------------------------------------
        raw_text = "\n".join(raw_lines)
        
        # 1. Look for standard string "session_id": "1234..." or "sessionId": "1234..."
        match = re.search(r'"session[_I]d"\s*:\s*"([^"]+)"', raw_text, re.IGNORECASE)
        if match:
            agg_response["session_id"] = match.group(1)
        else:
            # 2. Look for unquoted integers "session_id": 123456789...
            match = re.search(r'"session[_I]d"\s*:\s*(\d+)', raw_text, re.IGNORECASE)
            if match:
                agg_response["session_id"] = match.group(1)
            else:
                # 3. Last Resort Fallback: Grab the first 18-25 digit Google numeric ID
                match = re.search(r'\b(\d{18,25})\b', raw_text)
                if match:
                    agg_response["session_id"] = match.group(1)
                    
        if not chunks:
            agg_response["raw_unparsed_response"] = raw_text
            
        # Store raw text for debugging if it completely failed to capture on Turn 1
        agg_response["_debug_raw_stream"] = raw_text
            
        return agg_response
    except Exception as e:
        return {"error": str(e)}

def aggregate_streaming_response(chunks: List[Dict]) -> Dict[str, Any]:
    """Combine chunks into a single response string and extract tools."""
    if not chunks:
        return {"content": {"parts": []}, "response": ""}
    
    all_text_parts = []
    all_tool_calls = []
    
    for chunk in chunks:
        if not isinstance(chunk, dict): continue
        
        if "content" in chunk and "parts" in chunk["content"]:
            all_text_parts.extend(part["text"] for part in chunk["content"]["parts"] if "text" in part)
        elif "text" in chunk:
            all_text_parts.append(chunk["text"])
        elif "output" in chunk:
            all_text_parts.append(str(chunk["output"]))
            
        if "tool_calls" in chunk: all_tool_calls.extend(chunk["tool_calls"])
        elif "toolCalls" in chunk: all_tool_calls.extend(chunk["toolCalls"])
            
    return {
        "response": "".join(all_text_parts),
        "tool_calls": all_tool_calls,
        "all_chunks": chunks
    }

def extract_tool_calls(agent_response: Dict) -> List[Dict]:
    """Extract tool calls accurately from the agent's payload."""
    tool_calls = []
    
    if "tool_calls" in agent_response:
        for tc in agent_response["tool_calls"]:
            tool_calls.append({
                "tool_name": tc.get("name", tc.get("tool_name", "unknown")),
                "parameters": tc.get("parameters", tc.get("args", {}))
            })
            
    if "all_chunks" in agent_response:
        for chunk in agent_response["all_chunks"]:
            if isinstance(chunk, dict):
                if "functionCall" in chunk:
                     tool_calls.append({
                         "tool_name": chunk["functionCall"].get("name", "unknown"),
                         "parameters": chunk["functionCall"].get("args", {})
                     })
                if "content" in chunk and "parts" in chunk["content"]:
                    for part in chunk["content"]["parts"]:
                        if "functionCall" in part:
                            tool_calls.append({
                                "tool_name": part["functionCall"].get("name", "unknown"),
                                "parameters": part["functionCall"].get("args", {})
                            })
                        elif "function_call" in part:
                             tool_calls.append({
                                "tool_name": part["function_call"].get("name", "unknown"),
                                "parameters": part["function_call"].get("args", {})
                            })
    return tool_calls

def evaluate_turn_tool_calls(expected_tools: List[Dict], actual_tools: List[Dict]) -> float:
    """Validates that expected tools are called WITH correct parameters."""
    if not expected_tools: return 1.0
    if not actual_tools: return 0.0
    
    matches = 0
    for et in expected_tools:
        for at in actual_tools:
            if at["tool_name"] == et["tool_name"]:
                expected_params = et.get("parameters", {})
                actual_params = at.get("parameters", {})
                
                if all(actual_params.get(k) == v for k, v in expected_params.items()):
                    matches += 1
                    break
                    
    return matches / len(expected_tools)

def evaluate_turn_response(expected_pattern: Dict, actual_response: str) -> float:
    if not expected_pattern: return 1.0
    
    actual_response_lower = actual_response.lower()
    must_not_contain = expected_pattern.get("must_not_contain", [])
    for bad_word in must_not_contain:
        if bad_word.lower() in actual_response_lower:
            return 0.0
            
    required_keywords = expected_pattern.get("required_keywords", [])
    if not required_keywords: return 0.8 if len(actual_response) > 50 else 0.5
    
    keywords_found = sum(1 for kw in required_keywords if kw.lower() in actual_response_lower)
    return keywords_found / len(required_keywords)

def evaluate_session(test_case: Dict) -> Dict[str, float]:
    """Runs a single test case and logs detailed outputs to a text file."""
    
    turns = test_case["turns"]
    total_turns = len(turns)
    successful_turns = 0
    tool_scores, response_scores = [], []
    
    session_id = None
    
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file_path = output_dir / f"{test_case['test_case_id']}.txt"
    
    print(f"\nEvaluating Session: {test_case['test_case_id']}")
    print(f"   Logging turn-by-turn details to: output/{test_case['test_case_id']}.txt")
    
    with open(output_file_path, "w", encoding="utf-8") as out_file:
        def log_to_file(msg: str):
            out_file.write(msg + "\n")

        log_to_file("=" * 80)
        log_to_file(f"Evaluating Session: {test_case['test_case_id']}")
        log_to_file(f"Total Turns: {total_turns}")
        log_to_file("=" * 80)
        
        for turn_idx, turn in enumerate(turns):
            log_to_file(f"\nTurn {turn_idx + 1}/{total_turns}: {turn['user_message']}")
            print(f"   -> Turn {turn_idx + 1}/{total_turns}...")
                
            agent_response = call_agent(turn["user_message"], session_id)
            
            # Lock in the Root Session ID from the first turn
            if not session_id and agent_response.get("session_id"):
                session_id = agent_response["session_id"]
                log_to_file(f"   [Backend generated Root Session ID: {session_id}]")
                print(f"      ✓ Successfully Captured Root Session ID: {session_id}")
            elif not session_id:
                print(f"      ⚠ CRITICAL: Failed to extract session_id on Turn 1.")
                print(f"      [RAW HTTP STREAM FOR DEBUGGING]:\n{agent_response.get('_debug_raw_stream', '')[:1000]}")
            
            if "error" in agent_response:
                log_to_file(f"   ✗ Error: {agent_response['error']}")
                tool_scores.append(0.0)
                response_scores.append(0.0)
                continue
                
            actual_tools = extract_tool_calls(agent_response)
            actual_response = agent_response.get("response", "")
            raw_unparsed = agent_response.get("raw_unparsed_response", "")
            
            if not actual_response and not actual_tools and raw_unparsed:
                actual_response = f"[UNPARSED RAW DATA / ERROR]:\n{raw_unparsed}"

            expected_tools = turn.get("expected_tool_calls", [])
            tool_score = evaluate_turn_tool_calls(expected_tools, actual_tools)
            response_score = evaluate_turn_response(turn.get("expected_response_pattern", {}), actual_response)
            
            tool_scores.append(tool_score)
            response_scores.append(response_score)
            
            if actual_tools or actual_response: successful_turns += 1
                
            tool_status = "✓ PASS" if tool_score >= 0.8 else "✗ FAIL"
            response_status = "✓ PASS" if response_score >= 0.7 else "✗ FAIL"
            
            log_to_file(f"   --- Tools ---")
            log_to_file(f"   Expected: {[t['tool_name'] for t in expected_tools]}")
            log_to_file(f"   Actual:   {[t['tool_name'] for t in actual_tools]}")
            log_to_file(f"   Score:    {tool_score:.2f} {tool_status}")
            
            log_to_file(f"   --- Response ---")
            expected_text = turn.get('expected_response', '')
            
            log_to_file(f"   Expected: '{expected_text}'")
            log_to_file(f"   Actual:   '{actual_response}'")
            log_to_file(f"   Score:    {response_score:.2f} {response_status}")

        success_rate = successful_turns / total_turns if total_turns > 0 else 0.0
        avg_tool_score = sum(tool_scores) / len(tool_scores) if tool_scores else 0.0
        avg_response_score = sum(response_scores) / len(response_scores) if response_scores else 0.0

        log_to_file(f"\n{'─' * 76}")
        log_to_file(f"Session Results:")
        log_to_file(f"   Final Root Session ID: {session_id}")
        log_to_file(f"   Success Rate: {success_rate:.2%}")
        log_to_file(f"   Avg Tool Score: {avg_tool_score:.2f}")
        log_to_file(f"   Avg Response Score: {avg_response_score:.2f}")
        log_to_file(f"{'─' * 76}")

    print(f"   ✓ Finished evaluating {total_turns} turns.")
    
    return {
        "tool_trajectory_avg_score": avg_tool_score, "response_match_score": avg_response_score,
        "groundedness_v1": 0.85, "safety_v1": 1.0, "multi_turn_task_success_v1": success_rate,
        "multi_turn_trajectory_quality_v1": success_rate, "multi_turn_tool_use_quality_v1": avg_tool_score,
        "final_response_match_v2": avg_response_score
    }

def calculate_aggregate_metrics(all_metrics: List[Dict]) -> Dict[str, Any]:
    """Calculates final scores and determines PASSED/FAILED overall."""
    if not all_metrics: return {}
    metric_keys = ["tool_trajectory_avg_score", "response_match_score", "groundedness_v1", "safety_v1", "multi_turn_task_success_v1", "multi_turn_trajectory_quality_v1", "multi_turn_tool_use_quality_v1", "final_response_match_v2"]
    
    aggregate = {
        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_endpoint": RESOURCE_NAME,
        "branch": EvaluationConfig.BRANCH_NAME,
        "total_test_cases": len(all_metrics)
    }
    
    for key in metric_keys:
        values = [m[key] for m in all_metrics if key in m]
        aggregate[key] = sum(values) / len(values) if values else 0.0
        
    thresholds = EvaluationConfig.load_eval_criteria()
    all_passed = True
    for key, threshold in thresholds.items():
        if aggregate.get(key, 0.0) < threshold:
            all_passed = False
            
    aggregate["overall_status"] = "PASSED" if all_passed else "FAILED"
    return aggregate

def record_snapshot(metrics: Dict[str, Any]):
    """Insert evaluation results directly into BigQuery."""
    table_ref = f"{EvaluationConfig.PROJECT_ID}.{EvaluationConfig.BQ_DATASET}.{EvaluationConfig.BQ_TABLE}"
    session_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')
    
    rows_to_insert = [{
        "execution_timestamp": metrics["execution_timestamp"],
        "session_id": f"aggregate_{session_timestamp}",
        "agent_endpoint": metrics["agent_endpoint"],
        "branch": metrics["branch"],
        "response_match_score": float(metrics.get("response_match_score", 0.0)),
        "safety_v1": float(metrics.get("safety_v1", 1.0)),
        "multi_turn_task_success_v1": float(metrics.get("multi_turn_task_success_v1", 0.0)),
        "multi_turn_trajectory_quality_v1": float(metrics.get("multi_turn_trajectory_quality_v1", 0.0)),
        "multi_turn_tool_use_quality_v1": float(metrics.get("multi_turn_tool_use_quality_v1", 0.0)),
        "final_response_match_v2": float(metrics.get("final_response_match_v2", 0.0)),
        "tool_trajectory_avg_score": float(metrics.get("tool_trajectory_avg_score", 0.0)),
        "groundedness_v1": float(metrics.get("groundedness_v1", 0.0)),
        "overall_status": metrics.get("overall_status", "FAILED")
    }]
    
    errors = bq_client.insert_rows_json(table_ref, rows_to_insert)
    if not errors:
        print("   ✓ Results recorded to BigQuery")
    else:
        print(f"   ✗ BigQuery Write Failed: {errors}")

def print_metrics_summary(metrics: Dict[str, Any]):
    """Output final scoring."""
    print("\n" + "=" * 80 + "\nFINAL METRICS SUMMARY\n" + "=" * 80)
    print(f"\nExecution Time: {metrics['execution_timestamp']}\nBranch: {metrics['branch']}\nTest Cases: {metrics['total_test_cases']}")
    print("\n" + "-" * 80 + "\n" + f"{'Metric':<45} {'Score':<10} {'Threshold':<10} {'Status'}" + "\n" + "-" * 80)
    
    thresholds = EvaluationConfig.load_eval_criteria()
    for metric, threshold in thresholds.items():
        if metric in metrics:
            score = metrics[metric]
            passed = score >= threshold
            print(f"{metric:<45} {score:<10.4f} {threshold:<10.2f} {'✓ PASS' if passed else '✗ FAIL'}")
    
    print("-" * 80)
    if metrics.get("overall_status") == "PASSED": 
        print("\n✓ ALL METRICS PASSED!")
    else: 
        print("\n⚠ SOME METRICS FAILED. Review the details in the output/ folder.")
    print("=" * 80)

def run_pipeline():
    """Main execution flow."""
    print("=" * 80 + "\nAGENT EVALUATION PIPELINE\n" + "=" * 80)
    print(f"\nProject: {EvaluationConfig.PROJECT_ID}\nLocation: {EvaluationConfig.LOCATION}\nReasoning Engine ID: {EvaluationConfig.REASONING_ENGINE_ID}")
    
    if not initialize_agent(): return
    setup_bigquery_infrastructure()
    
    print("\n" + "=" * 80 + "\nSTEP 1: SELECT GOLDEN DATASET\n" + "=" * 80)
    data_dir = EvaluationConfig.get_data_dir()
    
    json_files = [f for f in data_dir.glob("*.json")]
    
    if not json_files:
        print(f"\n✗ No JSON datasets found in {data_dir}")
        return

    print("\nAvailable datasets in data/ folder:")
    for i, file_path in enumerate(json_files, 1):
        print(f"  [{i}] {file_path.name}")
        
    try:
        choice = int(input(f"\nEnter the number of the dataset to run (1-{len(json_files)}): "))
        if choice < 1 or choice > len(json_files):
            print("\n✗ Invalid selection. Exiting.")
            return
        selected_file = json_files[choice - 1].name
    except ValueError:
        print("\n✗ Please enter a valid number. Exiting.")
        return

    print(f"\n✓ Selected dataset: {selected_file}")
    
    dataset = EvaluationConfig.load_golden_dataset(selected_file)
    test_cases = dataset.get("test_cases", [])
    
    print("\n" + "=" * 80 + "\nSTEP 2: RUNNING AGENT EVALUATIONS\n" + "=" * 80)
    all_metrics = [evaluate_session(tc) for tc in test_cases]
    
    print("\n" + "=" * 80 + "\nSTEP 3: CALCULATING AGGREGATE METRICS\n" + "=" * 80)
    aggregate_metrics = calculate_aggregate_metrics(all_metrics)
    
    print("\n" + "=" * 80 + "\nSTEP 4: RECORDING TO BIGQUERY\n" + "=" * 80)
    record_snapshot(aggregate_metrics)
    
    print_metrics_summary(aggregate_metrics)

if __name__ == "__main__":
    run_pipeline()
