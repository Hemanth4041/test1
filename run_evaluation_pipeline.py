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

# Evaluation criteria thresholds — single source of truth
EVAL_CRITERIA: Dict[str, float] = {
    "tool_trajectory_avg_score":        0.8,
    "response_match_score":             0.5,
    "groundedness_v1":                  0.8,
    "safety_v1":                        1.0,
    "multi_turn_task_success_v1":       0.8,
    "multi_turn_trajectory_quality_v1": 0.8,
    "multi_turn_tool_use_quality_v1":   0.8,
    "final_response_match_v2":          0.5,
}

credentials    = None
session_client = None
execution_client = None

# ── ANSI helpers ──────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

def _pf_str(value: float, threshold: float) -> str:
    return f"{_GREEN}PASS{_RESET}" if value >= threshold else f"{_RED}FAIL{_RESET}"

def _bar(value: float, width: int = 20) -> str:
    filled = int(round(min(max(value, 0.0), 1.0) * width))
    return "█" * filled + "░" * (width - filled)


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
        print(f"{_RED}✗ Error initialising agent client: {e}{_RESET}")
        return False


# ── GOLDEN DATASET ────────────────────────────────────────────────────────────

def load_golden_dataset() -> Optional[Dict[str, Any]]:
    try:
        with open(EvaluationConfig.get_golden_dataset_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"{_RED}✗ Golden dataset not found at {EvaluationConfig.get_golden_dataset_path()}{_RESET}")
        return None
    except json.JSONDecodeError:
        print(f"{_RED}✗ Golden dataset is not valid JSON.{_RESET}")
        return None


# ── SESSION EVENT EXTRACTION ──────────────────────────────────────────────────
#
# Vertex AI session events have this structure per conversational turn:
#
#   EVENT  author=user    → user message text
#   EVENT  author=model   → functionCall parts  (tool invocation, no text)
#   EVENT  author=tool    → functionResponse parts
#   EVENT  author=model   → text parts          (final reply)
#
# The old code paired user→first-model-event, which captured tool calls but
# missed the response text that arrives in a LATER model event.
# Fix: collect ALL model events that follow a user event until the next user
# event, then merge their text and tool calls into one turn.

_USER_ROLES  = {"user", "human", "1"}
_AGENT_ROLES = {"agent", "model", "assistant", "0"}
_TOOL_ROLES  = {"tool", "function", "functionresponse"}


def _role_type(raw_role: str) -> str:
    r = raw_role.lower().strip()
    if r in _USER_ROLES  or "user"  in r or "human"    in r: return "user"
    if r in _TOOL_ROLES  or "tool"  in r or "function" in r: return "tool"
    if r in _AGENT_ROLES or "model" in r or "agent"    in r or "assistant" in r: return "agent"
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
        print(f"{_RED}✗ Error fetching session events: {e}{_RESET}")
        return []


def fetch_session_responses(session_id: str) -> List[Dict[str, Any]]:
    """
    Parse a session's event stream into a list of complete conversational turns.

    Each turn = {user_message, agent_response, tool_calls}

    Strategy:
      • Walk events in order.
      • On a USER event  → start a new pending turn, flush the previous one.
      • On an AGENT event → accumulate text + tool calls into the pending turn.
      • On a TOOL event  → skip (it's the tool's raw response, not the agent reply).
      • At end of stream → flush the last pending turn.
    """
    events = fetch_session_events(session_id)
    if not events:
        return []

    print(f"  Raw events fetched: {len(events)}")

    turns: List[Dict[str, Any]] = []

    # pending turn buffer
    pending_user   : Optional[str]       = None
    pending_text   : List[str]           = []
    pending_tools  : List[Dict[str, Any]] = []

    def _flush() -> None:
        """Commit the current pending turn if we have a user message."""
        if pending_user is not None:
            turns.append({
                "user_message":   pending_user,
                "agent_response": " ".join(pending_text).strip(),
                "tool_calls":     list(pending_tools),
            })

    for idx, event in enumerate(events):
        rr   = _raw_role(event)
        kind = _role_type(rr)
        parts = _parts_from_event(event)
        text  = _extract_text(parts)
        tools = _extract_tool_calls(parts)

        print(f"  Event {idx:02d}  role={rr:<14}  tools={len(tools)}  text_len={len(text)}")

        if kind == "user":
            # Flush previous turn before starting a new one
            _flush()
            pending_user  = text
            pending_text  = []
            pending_tools = []

        elif kind == "agent":
            # Accumulate — there may be multiple agent events per turn
            # (one with functionCall, another with the final text reply)
            if text:
                pending_text.append(text)
            pending_tools.extend(tools)

        # tool-role events are skipped

    # Flush the last turn
    _flush()

    print(f"  Turns extracted: {len(turns)}\n")
    return turns


# ── MODE 1 — EVALUATE EXISTING SESSION ───────────────────────────────────────

def evaluate_session_id(session_id: str, golden_test_case: Dict) -> Dict[str, Any]:
    golden_turns  = golden_test_case["turns"]
    session_turns = fetch_session_responses(session_id)

    if not session_turns:
        print(f"{_RED}  No turns could be extracted from session {session_id}.{_RESET}")
        m = _zero_metrics()
        m["session_id"] = session_id
        return m

    return _score_turns(
        session_id=session_id,
        golden_turns=golden_turns,
        actual_turns=session_turns,
    )


# ── MODE 2 — LIVE SESSION ─────────────────────────────────────────────────────

def create_session() -> Optional[str]:
    try:
        op      = session_client.create_session(
            parent=RESOURCE_NAME,
            session=aiplatform_v1b.Session(user_id=EvaluationConfig.EVAL_USER_ID),
        )
        session = op.result(timeout=60)
        return session.name
    except Exception as e:
        print(f"{_RED}✗ Error creating session: {e}{_RESET}")
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
    text_parts: List[str]       = []
    tool_calls: List[Dict]      = []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        # text
        if "content" in chunk and "parts" in chunk.get("content", {}):
            for part in chunk["content"]["parts"]:
                if "text" in part:
                    text_parts.append(part["text"])
                # function calls inside content parts
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

        # top-level tool_calls
        for key in ("tool_calls", "toolCalls"):
            for tc in chunk.get(key, []):
                tool_calls.append({
                    "tool_name":  tc.get("name", tc.get("tool_name", "unknown")),
                    "parameters": tc.get("parameters", tc.get("args", {})),
                })

        # functionCall at top level
        if "functionCall" in chunk:
            tool_calls.append({
                "tool_name":  chunk["functionCall"].get("name", "unknown"),
                "parameters": chunk["functionCall"].get("args", {}),
            })

    # de-duplicate tool calls
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
    print(f"  {_CYAN}Live session: {bare_session_id}{_RESET}\n")

    actual_turns: List[Dict] = []
    total = len(golden_turns)

    for i, golden_turn in enumerate(golden_turns):
        msg = golden_turn["user_message"]
        print(f"  [{i+1}/{total}] Sending: {msg[:70]}{'…' if len(msg)>70 else ''}")
        resp = call_agent(msg, session_name)

        if "error" in resp:
            print(f"  {_RED}  ✗ ERROR: {resp['error']}{_RESET}")
            actual_turns.append({"user_message": msg, "agent_response": "", "tool_calls": [], "error": resp["error"]})
        else:
            actual_turns.append({
                "user_message":   msg,
                "agent_response": resp.get("response", ""),
                "tool_calls":     resp.get("tool_calls", []),
            })

    print()
    return _score_turns(
        session_id=bare_session_id,
        golden_turns=golden_turns,
        actual_turns=actual_turns,
    )


# ── CORE SCORING ENGINE ───────────────────────────────────────────────────────

def _score_turns(
    session_id: str,
    golden_turns: List[Dict],
    actual_turns: List[Dict],
) -> Dict[str, Any]:
    total_turns = len(golden_turns)
    tool_scores, response_scores, safety_scores, groundedness_scores = [], [], [], []
    successful_turns    = 0
    perfect_tool_turns  = 0   # turns where tool_score == 1.0

    _section("TURN-BY-TURN EVALUATION")

    for i, golden_turn in enumerate(golden_turns):
        actual       = actual_turns[i] if i < len(actual_turns) else {}
        actual_text  = actual.get("agent_response", "")
        actual_tools = actual.get("tool_calls", [])
        has_error    = "error" in actual

        exp_tools    = golden_turn.get("expected_tool_calls", [])
        exp_pattern  = golden_turn.get("expected_response_pattern", {})
        exp_response = golden_turn.get("expected_response", "")

        t_score = _score_tool_calls(exp_tools, actual_tools)
        r_score = _score_response(exp_response, actual_text, exp_pattern)
        s_score = _score_safety(actual_text)
        g_score = _score_groundedness(actual_text, exp_response)

        tool_scores.append(t_score)
        response_scores.append(r_score)
        safety_scores.append(s_score)
        groundedness_scores.append(g_score)

        if t_score >= 1.0:
            perfect_tool_turns += 1
        if actual_tools or actual_text:
            successful_turns += 1

        # ── Turn output ───────────────────────────────────────────────────
        print(f"\n  {_BOLD}Turn {i+1} / {total_turns}{_RESET}  {'─'*52}")
        print(f"  {_CYAN}User:{_RESET} {golden_turn['user_message']}\n")

        exp_names = [t["tool_name"] for t in exp_tools]
        act_names = [t["tool_name"] for t in actual_tools]

        print(f"  {_BOLD}Expected tools   :{_RESET} {exp_names or '(none)'}")
        _print_truncated("  Expected response:", exp_response)

        if has_error:
            print(f"\n  {_RED}✗ Agent error: {actual['error']}{_RESET}")
        else:
            print(f"\n  {_BOLD}Actual tools     :{_RESET} {act_names or '(none)'}")
            _print_truncated("  Actual response:", actual_text)

        # Per-turn score bars
        print(f"\n  {'─'*66}")
        _score_row("Tool calls",     t_score)
        _score_row("Response match", r_score)
        _score_row("Safety",         s_score)
        _score_row("Groundedness",   g_score)

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

def _score_tool_calls(expected: List[Dict], actual: List[Dict]) -> float:
    """
    Score tool-call accuracy for one turn.

    Rules:
    - No expected tools → 1.0 (correct to not call any tool)
    - No actual tools   → 0.0
    - Per expected tool: full credit (1.0) if name + all expected params match;
      partial credit (0.5) if name matches but params don't;
      zero if tool not called at all.
    """
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
            continue  # tool not called → 0 credit

        ep = et.get("parameters", {})
        if not ep:
            score += 1.0   # name match, no params required
            continue

        # Check if any candidate fully satisfies all expected params
        full_match = any(
            all(c.get("parameters", {}).get(k) == v for k, v in ep.items())
            for c in candidates
        )
        score += 1.0 if full_match else 0.5   # partial credit for name match

    return min(score / len(expected), 1.0)


def _score_response(expected_response: str, actual: str, pattern: Dict) -> float:
    """
    Keyword-overlap score between expected and actual response.
    Hard-fails on must_not_contain violations.
    """
    if not actual:
        return 0.0

    lower_actual = actual.lower()

    for bad in pattern.get("must_not_contain", []):
        if bad.lower() in lower_actual:
            return 0.0

    if not expected_response:
        return 0.8 if len(actual) > 50 else 0.5

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
    if not actual:
        return 0.0
    if not expected_response:
        return 1.0
    anchors = [
        w.strip(".,!?|:()[]#*-").lower()
        for w in expected_response.split()
        if len(w.strip(".,!?|:()[]#*-")) > 4
    ]
    if not anchors:
        return 1.0
    found = sum(1 for w in anchors if w in actual.lower())
    return found / len(anchors)


def _zero_metrics() -> Dict[str, Any]:
    return {k: 0.0 for k in EVAL_CRITERIA}


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
    sr  = successful_turns  / n
    at  = sum(tool_scores)  / n
    ar  = sum(response_scores) / n
    ag  = sum(groundedness_scores) / n
    as_ = sum(safety_scores) / n
    tu  = perfect_tool_turns / n   # turns with perfect (1.0) tool score

    return {
        "session_id":                        session_id,
        "tool_trajectory_avg_score":         at,
        "response_match_score":              ar,
        "groundedness_v1":                   ag,
        "safety_v1":                         as_,
        "multi_turn_task_success_v1":        sr,
        "multi_turn_trajectory_quality_v1":  (sr + at + ar) / 3.0,
        "multi_turn_tool_use_quality_v1":    tu,
        "final_response_match_v2":           response_scores[-1] if response_scores else 0.0,
    }


# ── BIGQUERY ──────────────────────────────────────────────────────────────────

def calculate_aggregate_metrics(all_metrics: List[Dict]) -> Dict[str, Any]:
    if not all_metrics:
        return {}

    agg: Dict[str, Any] = {
        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_endpoint":      RESOURCE_NAME,
        "total_test_cases":    len(all_metrics),
    }

    for k in EVAL_CRITERIA:
        vals    = [m[k] for m in all_metrics if k in m]
        agg[k]  = sum(vals) / len(vals) if vals else 0.0

    # Overall: every metric must meet its threshold
    agg["overall_status"] = (
        "PASSED"
        if all(agg.get(k, 0.0) >= threshold for k, threshold in EVAL_CRITERIA.items())
        else "FAILED"
    )
    return agg


def record_snapshot(agg_metrics: Dict[str, Any], all_session_metrics: List[Dict]) -> None:
    """
    Insert one BQ row per session.

    Each tracked metric gets:
      <metric>         FLOAT  — raw score
      <metric>_status  STRING — "PASS" or "FAIL"

    All values are explicitly cast to the correct Python type so
    insert_rows_json doesn't hit schema-type mismatches.
    BQ insert errors are printed so silent failures are visible.
    """
    table_ref = (
        f"{EvaluationConfig.PROJECT_ID}."
        f"{EvaluationConfig.BQ_DATASET}."
        f"{EvaluationConfig.BQ_TABLE}"
    )

    rows: List[Dict[str, Any]] = []
    for m in all_session_metrics:
        row: Dict[str, Any] = {
            "execution_timestamp": str(agg_metrics["execution_timestamp"]),
            "session_id":          str(m.get("session_id", "unknown")),
            "agent_endpoint":      str(agg_metrics["agent_endpoint"]),
            "overall_status":      str(agg_metrics.get("overall_status", "FAILED")),
        }
        for metric_key, threshold in EVAL_CRITERIA.items():
            value = float(m.get(metric_key, 0.0))
            row[metric_key]                 = round(value, 6)          # FLOAT column
            row[f"{metric_key}_status"]     = "PASS" if value >= threshold else "FAIL"  # STRING column

        rows.append(row)

    errors = bq_client.insert_rows_json(table_ref, rows)
    if errors:
        print(f"{_RED}  ✗ BigQuery insert errors:{_RESET}")
        for err in errors:
            print(f"     {err}")
    else:
        print(f"  {_GREEN}✓ {len(rows)} row(s) written to BigQuery.{_RESET}")


# ── PRINT HELPERS ─────────────────────────────────────────────────────────────

def _section(title: str, width: int = 70) -> None:
    line = "═" * width
    print(f"\n{_BOLD}{line}\n  {title}\n{line}{_RESET}")


def _print_truncated(label: str, text: str, max_chars: int = 250) -> None:
    preview = (text[:max_chars] + "…") if len(text) > max_chars else text
    preview = preview.replace("\n", " ↵ ")
    print(f"  {label}")
    print(f"    {preview if preview else '(empty)'}")


def _score_row(label: str, value: float) -> None:
    bar = _bar(value)
    pct = f"{value:.0%}"
    print(f"  {label:<20} {bar}  {pct}")


def _print_summary(metrics: Dict[str, Any], agg: Dict[str, Any]) -> None:
    _section("EVALUATION SUMMARY")

    overall = agg.get("overall_status", "FAILED")
    colour  = _GREEN if overall == "PASSED" else _RED

    print(f"\n  Session ID : {_BOLD}{metrics['session_id']}{_RESET}")
    print(f"  Overall    : {colour}{_BOLD}{overall}{_RESET}\n")

    col_metric    = 38
    col_score     =  7
    col_bar       = 22
    col_threshold = 10

    header = (
        f"  {'Metric':<{col_metric}} {'Score':>{col_score}}  "
        f"{'':.<{col_bar}} {'Threshold':>{col_threshold}}  Status"
    )
    print(f"{_BOLD}  {'Metric':<{col_metric}} {'Score':>{col_score}}  {'Bar':<{col_bar}} {'Threshold':>{col_threshold}}  Status{_RESET}")
    print("  " + "─" * 86)

    labels = {
        "tool_trajectory_avg_score":          "Tool Trajectory Avg",
        "response_match_score":               "Response Match",
        "groundedness_v1":                    "Groundedness",
        "safety_v1":                          "Safety",
        "multi_turn_task_success_v1":         "Multi-Turn Task Success",
        "multi_turn_trajectory_quality_v1":   "Trajectory Quality",
        "multi_turn_tool_use_quality_v1":     "Tool Use Quality",
        "final_response_match_v2":            "Final Response Match",
    }

    for key, label in labels.items():
        value     = metrics.get(key, 0.0)
        threshold = EVAL_CRITERIA[key]
        bar       = _bar(value)
        pf        = _pf_str(value, threshold)
        print(
            f"  {label:<{col_metric}} {value:>{col_score}.2f}   "
            f"{bar:<{col_bar}} {threshold:>{col_threshold}.2f}   {pf}"
        )
    print()


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def run_pipeline() -> None:
    _section("AGENT EVALUATION PIPELINE")

    golden_data = load_golden_dataset()
    if not golden_data:
        return

    test_cases = golden_data.get("test_cases", [])
    if not test_cases:
        print(f"{_RED}✗ No test cases found in the golden dataset.{_RESET}")
        return

    golden_test_case = test_cases[0]
    print(f"\n  Golden dataset : {golden_test_case.get('test_case_id', 'unknown')}")
    print(f"  Description    : {golden_test_case.get('description', '')}")
    print(f"  Turns          : {len(golden_test_case['turns'])}")

    session_id = input(
        "\n  Enter a session ID to evaluate an existing session,\n"
        "  or press Enter to run a live session: "
    ).strip()

    print()

    if not initialize_agent():
        print(f"{_RED}✗ Could not initialise the agent client.{_RESET}")
        return

    if session_id:
        print(f"  Fetching responses from session {_BOLD}{session_id}{_RESET} …\n")
        metrics = evaluate_session_id(session_id, golden_test_case)
    else:
        print("  Starting live session evaluation …\n")
        metrics = evaluate_live_session(golden_test_case)

    agg = calculate_aggregate_metrics([metrics])

    _print_summary(metrics, agg)

    record_snapshot(agg, [metrics])


if __name__ == "__main__":
    run_pipeline()
