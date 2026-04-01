#!/usr/bin/env python3
"""
Claude Code Efficiency Analyzer
Reads your local Claude Code session logs and generates a usage + efficiency report.

Usage:
  python3 analyzer.py              # opens browser on port 8741
  python3 analyzer.py --port 9000  # custom port
  python3 analyzer.py --no-open    # don't auto-open browser
"""

import argparse
import glob
import json
import os
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Pricing (USD per million tokens, March 2026) ─────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# Cache write = 1.25x base input (5-min ephemeral), cache read = 0.1x base input
PRICING = {
    "claude-opus-4-6":            {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5-20250918":   {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-6":          {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5-20250929": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
}
FALLBACK_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_project_dirs():
    home = os.path.expanduser("~")
    base = os.path.join(home, ".claude", "projects")
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, d) for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]


def project_name_from_dir(dirpath):
    """Extract a human-readable project name from a .claude/projects/ directory.

    Directory names encode the cwd: /Users/me/Projects/foo → -Users-me-Projects-foo
    We read the first user record's cwd to get the real path, then shorten it.
    Falls back to parsing the directory name if no cwd is found.
    """
    # Try to read actual cwd from the first session file
    cwd = None
    jsonl_files = glob.glob(os.path.join(dirpath, "*.jsonl"))
    for fpath in jsonl_files[:3]:  # check up to 3 files
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("cwd"):
                            cwd = rec["cwd"]
                            break
                    except (json.JSONDecodeError, ValueError):
                        continue
            if cwd:
                break
        except Exception:
            continue

    if not cwd:
        # Fallback: reconstruct path from dirname by matching against known home prefix
        dirname = os.path.basename(dirpath).lstrip("-")
        home = os.path.expanduser("~")
        # The dirname encoding replaces both / and . with -
        # e.g. /Users/akshat.khandelwal/Desktop/foo → Users-akshat-khandelwal-Desktop-foo
        home_encoded = home.lstrip("/").replace("/", "-").replace(".", "-")
        if dirname == home_encoded:
            return "~ (home)"
        if dirname.startswith(home_encoded + "-"):
            rel = dirname[len(home_encoded) + 1:]  # e.g. Desktop-ai-native-pms
            return "~/" + rel
        return dirname

    home = os.path.expanduser("~")
    if cwd.rstrip("/") == home.rstrip("/"):
        return "~ (home)"
    if cwd.startswith(home + "/"):
        rel = cwd[len(home) + 1:]  # e.g. Projects/my-app
    else:
        rel = cwd

    # Show the last meaningful path segments
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return "~ (home)"

    # If path starts with common parent dirs, keep the project name + one level of context
    generic = {"Desktop", "Documents", "Projects", "repos", "src", "code", "dev", "work", "workspace"}
    # Find first non-generic component
    for i, p in enumerate(parts):
        if p not in generic:
            # Include one parent for context if it's generic (e.g. "Projects/my-app")
            if i > 0 and parts[i - 1] in generic:
                return "/".join(parts[i - 1:])
            return "/".join(parts[i:])
    # All parts are generic — show the last one
    return parts[-1]


def get_pricing(model):
    if not model:
        return FALLBACK_PRICING
    for key, p in PRICING.items():
        if key in model or model in key:
            return p
    ml = (model or "").lower()
    if "opus" in ml: return PRICING["claude-opus-4-6"]
    if "sonnet" in ml: return PRICING.get("claude-sonnet-4-6", FALLBACK_PRICING)
    if "haiku" in ml: return PRICING.get("claude-haiku-4-5-20251001", FALLBACK_PRICING)
    return FALLBACK_PRICING


def cost_of(model, usage):
    p = get_pricing(model)
    return (
        (usage.get("input_tokens", 0) / 1e6) * p["input"]
        + (usage.get("output_tokens", 0) / 1e6) * p["output"]
        + (usage.get("cache_read_input_tokens", 0) / 1e6) * p["cache_read"]
        + (usage.get("cache_creation_input_tokens", 0) / 1e6) * p["cache_write"]
    )


# ── Scanning ──────────────────────────────────────────────────────────────────

def scan_availability():
    """Quick scan to find date range and active dates across all logs."""
    dirs = find_project_dirs()
    files = []
    for d in dirs:
        files.extend(glob.glob(os.path.join(d, "*.jsonl")))

    active_dates = set()
    for fpath in files:
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = rec.get("timestamp")
                        if ts and (rec.get("type") in ("user", "assistant")):
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                            active_dates.add(str(dt))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception:
            continue

    if not active_dates:
        return {"first_date": None, "last_date": None, "active_dates": [], "total_files": len(files)}

    sorted_dates = sorted(active_dates)
    return {
        "first_date": sorted_dates[0],
        "last_date": sorted_dates[-1],
        "active_dates": sorted_dates,
        "total_files": len(files),
    }


def parse_session(filepath, date_from, date_to):
    session = {
        "session_id": None,
        "messages_user": 0, "messages_assistant": 0,
        "tokens_input": 0, "tokens_output": 0,
        "tokens_cache_read": 0, "tokens_cache_create": 0,
        "cost_usd": 0.0,
        "tool_calls": defaultdict(int),
        "model_tokens": defaultdict(int),
        "model_cost": defaultdict(float),
        "timestamps": [],
        "daily": defaultdict(lambda: {"tokens": 0, "cost": 0.0, "msgs": 0}),
        # Efficiency signals
        "reads": 0, "writes": 0, "mcp_calls": 0, "interrupts": 0,
        "mcp_tools": defaultdict(int),
        "mcp_tokens": 0, "mcp_tool_tokens": defaultdict(int),
        "repeated_reads": defaultdict(int),  # file_path -> count
        "max_agent_streak": 0, "_cur_streak": 0,
        "short_prompts": 0, "detailed_prompts": 0, "total_prompts": 0,
        "subagent_spawns": 0,
    }
    in_range = False
    cur_day = None
    _last_tool = None

    READ_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch"}
    WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}

    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = rec.get("type")
                ts_str = rec.get("timestamp")

                if ts_str and not session["session_id"]:
                    session["session_id"] = rec.get("sessionId")

                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        td = ts.date()
                        if date_from and td < date_from:
                            continue
                        if date_to and td > date_to:
                            continue
                        in_range = True
                        cur_day = str(td)
                        session["timestamps"].append(ts)
                    except (ValueError, TypeError):
                        pass

                if rtype == "user":
                    session["messages_user"] += 1
                    session["_cur_streak"] = 0  # reset agent streak
                    if cur_day:
                        session["daily"][cur_day]["msgs"] += 1
                    msg = rec.get("message", {})
                    content = msg.get("content", "")
                    # Track prompt length
                    prompt_len = 0
                    if isinstance(content, str):
                        prompt_len = len(content)
                    elif isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict):
                                if blk.get("type") == "text":
                                    prompt_len += len(blk.get("text", ""))
                                if "interrupted" in str(blk.get("text", "")).lower():
                                    session["interrupts"] += 1
                                if blk.get("type") == "tool_result":
                                    rc = blk.get("content", "")
                                    if isinstance(rc, str): sz = len(rc) // 4
                                    elif isinstance(rc, list): sz = sum(len(str(b)) for b in rc) // 4
                                    else: sz = len(str(rc)) // 4
                                    if _last_tool and "mcp__" in _last_tool:
                                        session["mcp_tokens"] += sz
                                        session["mcp_tool_tokens"][_last_tool] += sz
                    else:
                        if isinstance(content, str) and "interrupted" in content.lower():
                            session["interrupts"] += 1
                    if prompt_len > 0:
                        session["total_prompts"] += 1
                        if prompt_len < 20:
                            session["short_prompts"] += 1
                        elif prompt_len > 500:
                            session["detailed_prompts"] += 1

                elif rtype == "assistant":
                    session["messages_assistant"] += 1
                    # Track agent streaks (consecutive Claude turns)
                    session["_cur_streak"] += 1
                    session["max_agent_streak"] = max(session["max_agent_streak"], session["_cur_streak"])

                    msg = rec.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "unknown")

                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    session["tokens_input"] += inp
                    session["tokens_output"] += out
                    session["tokens_cache_read"] += cr
                    session["tokens_cache_create"] += cc

                    c = cost_of(model, usage)
                    session["cost_usd"] += c
                    ttok = inp + out + cr + cc
                    session["model_tokens"][model] += ttok
                    session["model_cost"][model] += c

                    if cur_day:
                        session["daily"][cur_day]["tokens"] += ttok
                        session["daily"][cur_day]["cost"] += c
                        session["daily"][cur_day]["msgs"] += 1

                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                                name = blk.get("name", "unknown")
                                _last_tool = name
                                session["tool_calls"][name] += 1
                                if name in READ_TOOLS:
                                    session["reads"] += 1
                                elif name in WRITE_TOOLS:
                                    session["writes"] += 1
                                if "mcp__" in name:
                                    session["mcp_calls"] += 1
                                    session["mcp_tools"][name] += 1
                                # Track repeated file reads
                                if name == "Read":
                                    fp = blk.get("input", {}).get("file_path", "") if isinstance(blk.get("input"), dict) else ""
                                    if fp:
                                        session["repeated_reads"][fp] += 1
                                # Track subagent spawns
                                if name == "Task":
                                    session["subagent_spawns"] += 1
    except Exception:
        return None

    if not in_range or session["messages_assistant"] == 0:
        return None
    if len(session["timestamps"]) >= 2:
        session["duration_min"] = (max(session["timestamps"]) - min(session["timestamps"])).total_seconds() / 60
    else:
        session["duration_min"] = 0

    # Derived metrics
    session["redundant_reads"] = sum(max(0, c - 1) for c in session["repeated_reads"].values())
    session["short_prompt_pct"] = session["short_prompts"] / max(session["total_prompts"], 1)

    # Compute per-session flags
    session["flags"] = []
    if session["reads"] > 5 and session["writes"] == 0:
        session["flags"].append("bulk-read-no-output")
    if session["mcp_calls"] > 20:
        session["flags"].append("mcp-heavy")
    if session["messages_user"] < 3 and session["messages_assistant"] > 10:
        session["flags"].append("low-interaction")
    if session["interrupts"] > 2:
        session["flags"].append("high-interrupts")
    if session["max_agent_streak"] >= 8:
        session["flags"].append("deep-agent-loop")
    if session["redundant_reads"] > 5:
        session["flags"].append("redundant-reads")

    return session


# ── Scoring ───────────────────────────────────────────────────────────────────

def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def score_usage(sessions, num_days):
    """Score 0-100 for how much you're using Claude Code."""
    if num_days <= 0 or not sessions:
        return 0

    active_days = len(set(str(d) for s in sessions for d in s["daily"].keys()))
    total_msgs = sum(s["messages_user"] + s["messages_assistant"] for s in sessions)
    total_sessions = len(sessions)

    # Active days ratio (what % of days did you use Claude?)
    day_ratio = active_days / num_days
    day_score = clamp(day_ratio * 125)  # 80% active = 100

    # Sessions per active day
    sess_per_day = total_sessions / max(active_days, 1)
    if sess_per_day >= 4: sess_score = 100
    elif sess_per_day >= 2: sess_score = 75
    elif sess_per_day >= 1: sess_score = 55
    else: sess_score = 30

    # Messages per active day
    msgs_per_day = total_msgs / max(active_days, 1)
    if msgs_per_day >= 100: msg_score = 100
    elif msgs_per_day >= 50: msg_score = 80
    elif msgs_per_day >= 20: msg_score = 60
    elif msgs_per_day >= 5: msg_score = 35
    else: msg_score = 15

    return clamp(round(day_score * 0.40 + sess_score * 0.30 + msg_score * 0.30))


def score_efficiency(sessions):
    """Score 0-100 for how efficiently you use Claude Code."""
    if not sessions:
        return 0

    total_input = sum(s["tokens_input"] for s in sessions)
    total_output = sum(s["tokens_output"] for s in sessions)
    total_cr = sum(s["tokens_cache_read"] for s in sessions)
    total_cc = sum(s["tokens_cache_create"] for s in sessions)
    total_asst = sum(s["messages_assistant"] for s in sessions)
    total_msgs = sum(s["messages_user"] + s["messages_assistant"] for s in sessions)

    # 1. Cache hit rate (25%)
    cache_denom = total_cr + total_cc + total_input
    cache_rate = total_cr / cache_denom if cache_denom > 0 else 0
    if cache_rate >= 0.85: cache_score = 100
    elif cache_rate >= 0.70: cache_score = 80
    elif cache_rate >= 0.55: cache_score = 60
    elif cache_rate >= 0.40: cache_score = 40
    else: cache_score = 20

    # 2. Messages per session — longer = better context amortization (15%)
    mps = total_msgs / len(sessions)
    if mps >= 80: mps_score = 100
    elif mps >= 40: mps_score = 80
    elif mps >= 15: mps_score = 60
    elif mps >= 5: mps_score = 35
    else: mps_score = 15

    # 3. Output tokens per message — leaner = better (15%)
    tpm = total_output / max(total_asst, 1)
    if tpm <= 200: tpm_score = 100
    elif tpm <= 500: tpm_score = 85
    elif tpm <= 1500: tpm_score = 65
    elif tpm <= 3000: tpm_score = 45
    else: tpm_score = 25

    # 4. Model cost optimization (15%)
    total_tok = total_input + total_output + total_cr + total_cc
    model_tokens = defaultdict(int)
    for s in sessions:
        for m, t in s["model_tokens"].items():
            model_tokens[m] += t
    opus_tok = sum(t for m, t in model_tokens.items() if "opus" in m.lower())
    opus_pct = opus_tok / total_tok if total_tok > 0 else 0
    if opus_pct <= 0.3: model_score = 100
    elif opus_pct <= 0.5: model_score = 80
    elif opus_pct <= 0.7: model_score = 60
    elif opus_pct <= 0.9: model_score = 40
    else: model_score = 25

    # 5. Read:Write ratio — producing output, not just consuming (15%)
    total_reads = sum(s["reads"] for s in sessions)
    total_writes = sum(s["writes"] for s in sessions)
    if total_writes == 0 and total_reads == 0:
        rw_score = 50  # no tool use — neutral
    elif total_writes == 0:
        rw_score = 20  # all reads, no output
    else:
        ratio = total_reads / total_writes
        if ratio <= 4: rw_score = 100    # healthy balance
        elif ratio <= 8: rw_score = 70   # read-heavy but some output
        elif ratio <= 15: rw_score = 45  # very read-heavy
        else: rw_score = 25             # almost pure consumption

    # 6. Session health — % of sessions without waste flags (15%)
    flagged = sum(1 for s in sessions if s.get("flags"))
    clean_pct = 1 - (flagged / len(sessions))
    health_score = clamp(round(clean_pct * 100))

    return clamp(round(
        cache_score * 0.25 + mps_score * 0.15 + tpm_score * 0.15
        + model_score * 0.15 + rw_score * 0.15 + health_score * 0.15
    ))


def composite_score(usage, efficiency):
    """Combine usage and efficiency — you need both to score well."""
    return clamp(round(usage * 0.40 + efficiency * 0.40 + min(usage, efficiency) * 0.20))


def tier_label(score):
    if score >= 90: return "Power User"
    if score >= 75: return "Solid"
    if score >= 60: return "Room to Grow"
    if score >= 45: return "Under-utilizing"
    if score >= 30: return "Needs Attention"
    return "Getting Started"


def usage_label(score):
    if score >= 75: return ("High", "Using Claude Code regularly across your workflow")
    if score >= 50: return ("Medium", "Moderate usage — room to integrate more")
    if score >= 25: return ("Low", "Light usage — try it for more tasks")
    return ("Very Low", "Barely using Claude Code")


def efficiency_label(score):
    if score >= 75: return ("High", "Strong cache reuse, lean responses, focused sessions")
    if score >= 50: return ("Medium", "Decent efficiency — some room to optimize")
    if score >= 25: return ("Low", "Burning more tokens than needed per task")
    return ("Very Low", "Significant room to improve how you use Claude")


def tier_desc(usage, efficiency):
    u_level = "high" if usage >= 65 else "medium" if usage >= 35 else "low"
    e_level = "high" if efficiency >= 65 else "medium" if efficiency >= 35 else "low"

    descs = {
        ("high", "high"): "You're using Claude Code frequently and efficiently. Keep it up.",
        ("high", "medium"): "Strong usage but some efficiency gains available. Check the recommendations below.",
        ("high", "low"): "You're using Claude Code a lot but not efficiently. Focus on the efficiency tips below — they'll save significant cost.",
        ("medium", "high"): "When you use Claude Code, you use it well. Consider using it for more of your workflow to get full value.",
        ("medium", "medium"): "Moderate usage and efficiency. Room to improve on both fronts.",
        ("medium", "low"): "Moderate usage but low efficiency. Focus on the efficiency recommendations before increasing usage.",
        ("low", "high"): "Efficient when you use it, but you're under-utilizing it. Try incorporating Claude Code into more of your daily work.",
        ("low", "medium"): "Light usage with average efficiency. You have room to grow on both dimensions.",
        ("low", "low"): "Just getting started. Try longer, more focused sessions and review the tips below.",
    }
    return descs.get((u_level, e_level), "")


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze(date_from, date_to):
    dirs = find_project_dirs()
    # Track which project each file belongs to
    files = []
    file_project = {}  # filepath -> project dir
    for d in dirs:
        for fpath in glob.glob(os.path.join(d, "*.jsonl")):
            files.append(fpath)
            file_project[fpath] = d

    sessions = []
    for fpath in files:
        s = parse_session(fpath, date_from, date_to)
        if s:
            s["_project_dir"] = file_project[fpath]
            sessions.append(s)

    # Prior period
    period_days = (date_to - date_from).days
    prev_from = date_from - timedelta(days=period_days + 1)
    prev_to = date_from - timedelta(days=1)
    prev_sessions = [s for fpath in files if (s := parse_session(fpath, prev_from, prev_to))]

    if not sessions:
        return {"error": "No sessions found in this date range.", "sessions": 0}

    # Aggregates
    ti = sum(s["tokens_input"] for s in sessions)
    to_ = sum(s["tokens_output"] for s in sessions)
    tcr = sum(s["tokens_cache_read"] for s in sessions)
    tcc = sum(s["tokens_cache_create"] for s in sessions)
    ttok = ti + to_ + tcr + tcc
    tcost = sum(s["cost_usd"] for s in sessions)
    t_user = sum(s["messages_user"] for s in sessions)
    t_asst = sum(s["messages_assistant"] for s in sessions)
    t_dur = sum(s["duration_min"] for s in sessions)
    t_tools = sum(sum(s["tool_calls"].values()) for s in sessions)

    all_tools = defaultdict(int)
    for s in sessions:
        for t, c in s["tool_calls"].items():
            all_tools[t] += c

    model_tokens = defaultdict(int)
    model_cost = defaultdict(float)
    for s in sessions:
        for m, t in s["model_tokens"].items(): model_tokens[m] += t
        for m, c in s["model_cost"].items(): model_cost[m] += c

    cache_denom = tcr + tcc + ti
    cache_rate = tcr / cache_denom if cache_denom > 0 else 0
    tpm = to_ / t_asst if t_asst > 0 else 0
    cps = tcost / len(sessions)
    mps = (t_user + t_asst) / len(sessions)
    num_days = (date_to - date_from).days + 1
    proj_monthly = (tcost / num_days) * 30 if num_days > 0 else tcost

    opus_tok = sum(t for m, t in model_tokens.items() if "opus" in m.lower())
    opus_pct = opus_tok / ttok if ttok > 0 else 0
    opus_cost = sum(c for m, c in model_cost.items() if "opus" in m.lower())

    # Cost breakdown by token type (approximate — uses dominant model pricing)
    dominant_model = max(model_tokens, key=model_tokens.get) if model_tokens else ""
    dp = get_pricing(dominant_model)
    cost_input = (ti / 1e6) * dp["input"]
    cost_output = (to_ / 1e6) * dp["output"]
    cost_cache_read = (tcr / 1e6) * dp["cache_read"]
    cost_cache_write = (tcc / 1e6) * dp["cache_write"]

    # Daily (per-message attribution)
    daily = defaultdict(lambda: {"sessions": set(), "tokens": 0, "cost": 0.0, "msgs": 0})
    for s in sessions:
        sid = s["session_id"] or id(s)
        for day_str, dd in s["daily"].items():
            daily[day_str]["sessions"].add(sid)
            daily[day_str]["tokens"] += dd["tokens"]
            daily[day_str]["cost"] += dd["cost"]
            daily[day_str]["msgs"] += dd["msgs"]
    daily_list = [{"date": k, "sessions": len(v["sessions"]), "tokens": v["tokens"], "cost": v["cost"], "msgs": v["msgs"]} for k, v in sorted(daily.items())]

    # Models
    model_list = [{"model": m, "tokens": t, "cost": model_cost.get(m, 0), "pct": t / ttok if ttok > 0 else 0} for m, t in sorted(model_tokens.items(), key=lambda x: -x[1])]

    # Tools
    sorted_tools = sorted(all_tools.items(), key=lambda x: -x[1])[:12]
    tools_list = [{"name": t.replace("mcp__", "").replace("__", "::"), "count": c} for t, c in sorted_tools]

    # ── Scores ──
    u_score = score_usage(sessions, num_days)
    e_score = score_efficiency(sessions)
    c_score = composite_score(u_score, e_score)

    # ── Recommendations (section-linked) ──
    recs = {"efficiency": [], "models": [], "tools": [], "daily": []}

    # Efficiency
    if cache_rate >= 0.80:
        recs["efficiency"].append({"type": "good", "title": "Excellent Cache Reuse", "body": f"Cache hit rate of {cache_rate:.0%} is excellent (benchmark: >80%). You're efficiently reusing context across messages."})
    elif cache_rate < 0.60:
        recs["efficiency"].append({"type": "warn", "title": "Low Cache Hit Rate", "body": f"Cache hit rate is {cache_rate:.0%} (benchmark: >60%). Try longer sessions — each new session re-processes your entire project context from scratch."})

    if mps >= 50:
        recs["efficiency"].append({"type": "good", "title": "Deep, Focused Sessions", "body": f"Averaging {mps:.0f} messages per session — deep, sustained work that amortizes context-loading costs."})
    elif mps < 5:
        recs["efficiency"].append({"type": "warn", "title": "Too Many Short Sessions", "body": f"Only {mps:.1f} messages per session. Each new session re-loads context from scratch. Batch related tasks together."})

    if 0 < tpm <= 300:
        recs["efficiency"].append({"type": "good", "title": "Lean Responses", "body": f"Output tokens per message ({tpm:.0f}) is lean — concise, targeted answers."})
    elif tpm > 5000:
        recs["efficiency"].append({"type": "info", "title": "Verbose Responses", "body": f"Output averages {tpm:,.0f} tokens/message. Try more specific prompts or asking for concise answers."})

    if cps > 5.0:
        recs["efficiency"].append({"type": "info", "title": "High Session Cost", "body": f"Average ${cps:.2f}/session. Long exploratory sessions burn tokens on context that gets compressed. Consider focused, task-specific sessions."})

    # Models
    if opus_pct > 0.80:
        savings = opus_cost * 0.6
        recs["models"].append({"type": "warn", "title": "High Opus Usage", "body": f"{opus_pct:.0%} of tokens on Opus (${opus_cost:.0f}). Routine tasks (file reads, simple edits) run fine on Sonnet at 60% lower cost — potential savings: ~${savings:.0f}."})
    elif opus_pct <= 0.5 and opus_pct > 0:
        recs["models"].append({"type": "good", "title": "Cost-Efficient Model Mix", "body": f"Only {opus_pct:.0%} Opus — you're matching model power to task complexity."})

    # Tools
    if t_tools > 0:
        code_tools = sum(c for t, c in all_tools.items() if t in ("Edit", "Write"))
        explore_tools = sum(c for t, c in all_tools.items() if t in ("Read", "Grep", "Glob", "Bash"))
        mcp_count = sum(c for t, c in all_tools.items() if "mcp__" in t)

        if code_tools > 0 and explore_tools > 0:
            ratio = explore_tools / code_tools
            if ratio > 5:
                recs["tools"].append({"type": "info", "title": "Exploration-Heavy", "body": f"{ratio:.0f}x more reading/searching ({explore_tools}) than editing ({code_tools}). Typical for research and code review. If you're writing code, provide more upfront context."})
            elif ratio < 0.5:
                recs["tools"].append({"type": "info", "title": "Write-Heavy", "body": f"More edits ({code_tools}) than reads ({explore_tools}). Heavy creation mode — Claude is actively producing artifacts."})
            else:
                recs["tools"].append({"type": "good", "title": "Balanced Read/Write", "body": f"Healthy mix of exploration ({explore_tools}) and creation ({code_tools}). Claude reads before writing."})

        # External integration detail is now in the consolidated health card

        bash_count = all_tools.get("Bash", 0)
        if bash_count > t_tools * 0.4:
            recs["tools"].append({"type": "info", "title": "Heavy Bash Usage", "body": f"Bash is {bash_count / t_tools:.0%} of tool calls. Claude has dedicated Read, Edit, Grep, Glob tools that are more token-efficient for file ops."})

    # Daily
    if proj_monthly > 200:
        recs["daily"].append({"type": "info", "title": "High Monthly Projection", "body": f"Projected ${proj_monthly:.0f}/mo. Benchmarks: moderate $30-50, power user $80-200. Not inherently bad if output justifies it."})
    if daily_list:
        peak = max(daily_list, key=lambda x: x["cost"])
        if peak["cost"] > tcost * 0.25 and tcost > 5:
            recs["daily"].append({"type": "info", "title": "Spending Spike", "body": f"Peak day ({peak['date']}) = {peak['cost'] / tcost:.0%} of total cost at ${peak['cost']:.2f}. Was it a justified deep-dive?"})

    # Trend
    trend = None
    if prev_sessions:
        p_out = sum(s["tokens_output"] for s in prev_sessions)
        p_asst = sum(s["messages_assistant"] for s in prev_sessions)
        p_tpm = p_out / p_asst if p_asst > 0 else 0
        p_cr = sum(s["tokens_cache_read"] for s in prev_sessions)
        p_cc = sum(s["tokens_cache_create"] for s in prev_sessions)
        p_inp = sum(s["tokens_input"] for s in prev_sessions)
        p_denom = p_cr + p_cc + p_inp
        p_cache = p_cr / p_denom if p_denom > 0 else 0
        p_cost = sum(s["cost_usd"] for s in prev_sessions)
        p_cps = p_cost / len(prev_sessions)
        trend = {
            "prev_tokens_per_msg": p_tpm, "curr_tokens_per_msg": tpm,
            "prev_cache_rate": p_cache, "curr_cache_rate": cache_rate,
            "prev_cost_per_session": p_cps, "curr_cost_per_session": cps,
            "prev_total_cost": p_cost, "curr_total_cost": tcost,
            "prev_sessions": len(prev_sessions), "curr_sessions": len(sessions),
        }

    # ── Session Health ──
    total_reads = sum(s["reads"] for s in sessions)
    total_writes = sum(s["writes"] for s in sessions)
    total_mcp = sum(s["mcp_calls"] for s in sessions)
    total_mcp_tokens = sum(s["mcp_tokens"] for s in sessions)
    total_interrupts = sum(s["interrupts"] for s in sessions)
    flagged_sessions = [s for s in sessions if s.get("flags")]
    rw_ratio = total_reads / max(total_writes, 1) if total_writes > 0 else (total_reads if total_reads > 0 else 0)

    # New deep signals
    total_redundant_reads = sum(s["redundant_reads"] for s in sessions)
    total_subagents = sum(s["subagent_spawns"] for s in sessions)
    total_short_prompts = sum(s["short_prompts"] for s in sessions)
    total_detailed_prompts = sum(s["detailed_prompts"] for s in sessions)
    total_prompts = sum(s["total_prompts"] for s in sessions)
    short_prompt_pct = total_short_prompts / max(total_prompts, 1)
    detailed_prompt_pct = total_detailed_prompts / max(total_prompts, 1)
    sessions_with_deep_loops = sum(1 for s in sessions if s["max_agent_streak"] >= 5)
    avg_agent_streak = sum(s["max_agent_streak"] for s in sessions) / len(sessions)

    # Aggregate MCP tokens by tool
    mcp_tool_tokens = defaultdict(int)
    for s in sessions:
        for t, tok in s["mcp_tool_tokens"].items():
            mcp_tool_tokens[t] += tok
    mcp_heavy_tools = sorted(
        [(t.replace("mcp__", "").replace("__", "::"), tok) for t, tok in mcp_tool_tokens.items()],
        key=lambda x: -x[1]
    )[:5]

    # Session health recommendations — consolidated, no duplicates
    FRIENDLY_MCP = {
        "playwright::browser_take_screenshot": "Screenshots",
        "granola::get_meeting_transcript": "Meeting Transcripts",
        "google-workspace::readGoogleDoc": "Google Docs",
        "figma::get_screenshot": "Figma Screenshots",
        "snowflake::run_snowflake_query": "Snowflake Queries",
        "snowflake::list_objects": "Snowflake Schema",
        "slack::slack_read_thread": "Slack Threads",
        "slack::slack_search_public": "Slack Search",
        "glean_default::read_document": "Glean Documents",
    }

    recs["health"] = []
    if flagged_sessions:
        flag_counts = defaultdict(int)
        for s in flagged_sessions:
            for f in s["flags"]:
                flag_counts[f] += 1

        if flag_counts.get("bulk-read-no-output", 0) > 0:
            n = flag_counts["bulk-read-no-output"]
            recs["health"].append({"type": "warn", "title": "Bulk Reads Without Output",
                "body": str(n) + " session(s) had 5+ file reads but zero edits or writes. If this is research, that's fine — but if not, it may be aimless browsing that burns tokens without producing anything."})

        if flag_counts.get("low-interaction", 0) > 0:
            n = flag_counts["low-interaction"]
            recs["health"].append({"type": "warn", "title": "Low-Interaction Sessions",
                "body": str(n) + " session(s) had fewer than 3 messages from you but 10+ from Claude. Claude may be running autonomously with minimal steering — check that you're guiding the work."})
    if not flagged_sessions:
        recs["health"].append({"type": "good", "title": "Clean Sessions", "body": "No sessions flagged for waste patterns. Your sessions are focused and productive."})

    # Consolidated MCP insight (merge MCP-heavy + high ratio + heavy consumption)
    if total_mcp > 0 and (total_mcp_tokens > 50000 or total_mcp > 100):
        friendly_top = ", ".join(FRIENDLY_MCP.get(n, n) + " (~" + str(tok // 1000) + "K tokens)" for n, tok in mcp_heavy_tools[:3])
        mcp_est_cost = total_mcp_tokens * dp["cache_write"] / 1e6  # rough: MCP results become cache writes
        recs["health"].append({"type": "warn", "title": "External Service Costs",
            "body": str(total_mcp) + " calls to external services consumed ~" + str(total_mcp_tokens // 1000) + "K tokens in results (est. ~$" + str(round(mcp_est_cost)) + "). Heaviest: " + friendly_top + ". Screenshots and full document reads are especially expensive — consider whether you need the full content or just a summary."})

    # Consolidated interrupt insight (merge frequent + high rate)
    if total_interrupts > len(sessions) * 0.2:
        recs["health"].append({"type": "warn", "title": "Frequent Interrupts",
            "body": str(total_interrupts) + " cancelled requests across " + str(len(sessions)) + " sessions. Each interrupt wastes the tokens Claude already spent generating its response. Being more specific upfront reduces the need to cancel mid-response."})

    if total_redundant_reads > 20:
        recs["health"].append({"type": "warn", "title": "Redundant File Reads",
            "body": str(total_redundant_reads) + " times a file was re-read in the same session when it was already in context. Try asking Claude to reference content it already loaded instead of re-reading files."})

    if sessions_with_deep_loops > len(sessions) * 0.3:
        recs["health"].append({"type": "info", "title": "Frequent Agent Loops",
            "body": str(sessions_with_deep_loops) + " sessions (" + str(round(sessions_with_deep_loops/len(sessions)*100)) + "%) had 5+ consecutive Claude turns without your input. Agent mode is powerful but burns tokens fast. Consider breaking complex tasks into steps you guide."})

    if short_prompt_pct > 0.15:
        recs["health"].append({"type": "warn", "title": "Vague Prompts",
            "body": str(round(short_prompt_pct * 100)) + "% of your prompts are under 20 characters. Short prompts force Claude to guess what you want — leading to longer, more expensive responses."})
    elif detailed_prompt_pct > 0.20:
        recs["health"].append({"type": "good", "title": "Specific Prompts",
            "body": str(round(detailed_prompt_pct * 100)) + "% of your prompts are 500+ characters. Detailed prompts help Claude get it right the first time."})

    # ── Per-Project Breakdown ──
    proj_groups = defaultdict(list)
    for s in sessions:
        proj_groups[s["_project_dir"]].append(s)

    projects = []
    for pdir, psessions in proj_groups.items():
        pname = project_name_from_dir(pdir)
        p_cost = sum(s["cost_usd"] for s in psessions)
        p_tok = sum(s["tokens_input"] + s["tokens_output"] + s["tokens_cache_read"] + s["tokens_cache_create"] for s in psessions)
        p_out = sum(s["tokens_output"] for s in psessions)
        p_msgs = sum(s["messages_user"] + s["messages_assistant"] for s in psessions)
        p_tools = sum(sum(s["tool_calls"].values()) for s in psessions)
        p_writes = sum(s["writes"] for s in psessions)
        p_reads = sum(s["reads"] for s in psessions)
        p_mcp = sum(s["mcp_calls"] for s in psessions)
        p_dur = sum(s["duration_min"] for s in psessions)
        p_eff = score_efficiency(psessions)
        projects.append({
            "name": pname,
            "sessions": len(psessions),
            "cost": p_cost,
            "tokens": p_tok,
            "output_tokens": p_out,
            "messages": p_msgs,
            "tool_calls": p_tools,
            "writes": p_writes,
            "reads": p_reads,
            "mcp_calls": p_mcp,
            "duration_min": p_dur,
            "efficiency": p_eff,
            "cost_pct": p_cost / tcost if tcost > 0 else 0,
        })
    projects.sort(key=lambda x: -x["cost"])

    return {
        "period": {"from": str(date_from), "to": str(date_to), "days": num_days},
        "summary": {
            "sessions": len(sessions), "total_tokens": ttok,
            "total_cost": tcost, "total_user_msgs": t_user,
            "total_asst_msgs": t_asst, "total_tool_calls": t_tools,
            "total_duration_min": t_dur,
            "tokens_input": ti, "tokens_output": to_,
            "tokens_cache_read": tcr, "tokens_cache_create": tcc,
        },
        "efficiency": {
            "tokens_per_msg": tpm, "cost_per_session": cps,
            "cache_hit_rate": cache_rate, "msgs_per_session": mps,
            "avg_duration_min": t_dur / len(sessions),
            "projected_monthly": proj_monthly,
        },
        "session_health": {
            "total_reads": total_reads, "total_writes": total_writes,
            "rw_ratio": rw_ratio, "total_mcp": total_mcp,
            "total_mcp_tokens": total_mcp_tokens,
            "avg_mcp_tokens": total_mcp_tokens // max(total_mcp, 1),
            "mcp_heavy_tools": mcp_heavy_tools,
            "total_interrupts": total_interrupts,
            "flagged_count": len(flagged_sessions),
            "clean_pct": round((1 - len(flagged_sessions) / len(sessions)) * 100),
            "redundant_reads": total_redundant_reads,
            "subagent_spawns": total_subagents,
            "short_prompt_pct": round(short_prompt_pct * 100),
            "detailed_prompt_pct": round(detailed_prompt_pct * 100),
            "sessions_with_agent_loops": sessions_with_deep_loops,
            "avg_agent_streak": round(avg_agent_streak, 1),
        },
        "scores": {
            "usage": u_score, "efficiency": e_score, "composite": c_score,
            "tier": tier_label(c_score), "description": tier_desc(u_score, e_score),
            "usage_label": usage_label(u_score)[0], "usage_desc": usage_label(u_score)[1],
            "eff_label": efficiency_label(e_score)[0], "eff_desc": efficiency_label(e_score)[1],
        },
        "cost_breakdown": {
            "input": cost_input, "output": cost_output,
            "cache_read": cost_cache_read, "cache_write": cost_cache_write,
        },
        "models": model_list, "opus_pct": opus_pct,
        "tools": tools_list, "daily": daily_list,
        "trend": trend, "recs": recs,
        "projects": projects,
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Efficiency Analyzer</title>
<style>
:root{--bg:#f5f6f8;--s1:#fff;--s2:#f0f1f4;--s3:#e5e7ee;--border:#dde0e8;--text:#1a1d2b;--muted:#6b7085;--dim:#9298b0;--accent:#6366f1;--green:#16a34a;--green-dim:rgba(22,163,74,.08);--yellow:#ca8a04;--yellow-dim:rgba(202,138,4,.08);--orange:#ea580c;--orange-dim:rgba(234,88,12,.08);--red:#dc2626;--blue:#2563eb;--blue-dim:rgba(37,99,235,.07)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;min-height:100vh}
.wrap{max-width:1140px;margin:0 auto;padding:28px 24px 60px}
.header{margin-bottom:24px}.header h1{font-size:22px;font-weight:700;margin-bottom:2px}.header p{font-size:13px;color:var(--muted)}
.avail{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:14px 18px;margin:16px 0;font-size:13px;color:var(--muted)}.avail strong{color:var(--text)}
.controls{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
.dw input[type="date"]{background:var(--s1);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;font-family:inherit;cursor:pointer;min-width:150px}
.dw input:focus{outline:none;border-color:var(--accent)}
button{padding:8px 20px;border-radius:6px;font-size:13px;cursor:pointer;border:none;font-family:inherit;font-weight:600;transition:all .15s}
.bp{background:var(--accent);color:#fff}.bp:hover{background:#4f46e5}
.bg{background:var(--s1);color:var(--muted);border:1px solid var(--border)}.bg:hover{color:var(--text);background:var(--s2)}.bg.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.sep{color:var(--dim);font-size:13px}
#status{font-size:12px;color:var(--muted);margin-left:8px}
.score-section{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:0;align-items:center;margin-bottom:24px}
.score-card{background:var(--s1);border:1px solid var(--border);border-radius:12px;padding:22px 18px;text-align:center}
.score-card.main{padding:22px 18px;border:2px solid var(--border);box-shadow:0 2px 12px rgba(0,0,0,.05)}
.score-num{font-size:48px;font-weight:800;line-height:1}.score-num.big{font-size:48px}
.score-label{font-size:11px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.score-tier{display:inline-block;margin-top:10px;padding:4px 16px;border-radius:20px;font-size:13px;font-weight:600}
.score-desc{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.4}
.score-sub{margin-top:8px;font-size:13px;font-weight:600}.score-subdesc{font-size:11px;color:var(--muted);margin-top:2px;line-height:1.4}
.score-op{font-size:24px;font-weight:300;color:var(--dim);text-align:center;padding:0 6px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.card{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:16px}
.card .v{font-size:24px;font-weight:800;line-height:1.1}.card .l{font-size:11px;color:var(--muted);margin-top:3px}.card .s{font-size:10px;color:var(--dim);margin-top:6px;padding-top:6px;border-top:1px solid var(--border)}
.tri-cols{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.panel{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:20px}.panel h3{font-size:14px;font-weight:600;margin-bottom:4px}.panel-sub{font-size:11px;color:var(--dim);margin-bottom:14px}
.gr{display:flex;align-items:center;gap:12px;margin-bottom:12px}.gl{width:110px;font-size:11px;color:var(--muted);text-align:right;flex-shrink:0}
.gt{flex:1;height:7px;background:var(--s3);border-radius:4px;overflow:hidden}.gf{height:100%;border-radius:4px;transition:width .6s ease}.gv{width:65px;font-size:12px;font-weight:600}
.br{display:flex;align-items:center;gap:8px;margin-bottom:5px}.bl{width:70px;font-size:10px;color:var(--muted);text-align:right;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bt{flex:1;height:20px;background:var(--s3);border-radius:3px;overflow:hidden}.bf{height:100%;border-radius:3px;display:flex;align-items:center;padding-left:6px;font-size:9px;font-weight:600;color:#fff;transition:width .5s ease;min-width:2px}
.bv{width:60px;font-size:10px;text-align:right;color:var(--muted);flex-shrink:0}
.tr{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--border)}.tr:last-child{border-bottom:none}
.tm{width:110px;font-size:12px;color:var(--muted)}.tv{font-size:13px;flex:1}
.ta{font-size:11px;font-weight:600;padding:2px 8px;border-radius:4px}
.ta-ug{background:var(--green-dim);color:var(--green)}.ta-ub{background:var(--orange-dim);color:var(--orange)}.ta-dg{background:var(--green-dim);color:var(--green)}.ta-db{background:var(--orange-dim);color:var(--orange)}.ta-s{background:var(--blue-dim);color:var(--blue)}
.rec{padding:11px 14px;border-radius:8px;margin-bottom:7px;border-left:3px solid}
.rec-good{background:var(--green-dim);border-color:var(--green)}.rec-warn{background:var(--yellow-dim);border-color:var(--yellow)}.rec-info{background:var(--blue-dim);border-color:var(--blue)}
.rec h4{font-size:12px;font-weight:600;margin-bottom:2px}.rec p{font-size:11px;color:var(--muted);line-height:1.4}
.rec-good h4{color:var(--green)}.rec-warn h4{color:var(--yellow)}.rec-info h4{color:var(--blue)}
.srecs{margin-top:12px}
.loading{text-align:center;padding:60px 0;color:var(--muted);font-size:14px}.hidden{display:none}
.footer{text-align:center;padding:24px 0;font-size:11px;color:var(--dim)}.footer a{color:var(--accent);text-decoration:none}.footer code{background:var(--s2);padding:1px 5px;border-radius:3px;font-size:10px}
@media(max-width:900px){.tri-cols{grid-template-columns:1fr}}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}.score-section{grid-template-columns:1fr;gap:12px}.score-op{display:none}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header"><h1>Claude Code Efficiency Analyzer</h1><p>Analyzes your local Claude Code session logs to help you understand your spending, token distribution, and where you can be more efficient.</p></div>
  <div id="avail" class="avail">Scanning local logs...</div>
  <div class="controls">
    <div class="dw"><input type="date" id="df" onclick="this.showPicker&&this.showPicker()"></div>
    <span class="sep">to</span>
    <div class="dw"><input type="date" id="dt" onclick="this.showPicker&&this.showPicker()"></div>
    <button class="bp" onclick="run()">Analyze</button>
    <button class="bg" onclick="preset(7)">7d</button>
    <button class="bg" onclick="preset(14)">14d</button>
    <button class="bg active" onclick="preset(30)">30d</button>
    <button class="bg" onclick="preset(0)">All</button>
    <span id="status"></span>
  </div>
  <div id="dash" class="hidden"></div>
  <div id="loading" class="loading hidden"></div>
  <div class="footer">Reads <code>~/.claude/projects/</code> only. No data leaves your machine. Cost estimates use <a href="https://platform.claude.com/docs/en/about-claude/pricing" target="_blank">Anthropic published pricing</a> — enterprise rates may differ.</div>
</div>
<script>
const $=s=>document.querySelector(s);let AV=null;
async function init(){try{const r=await fetch('/api/availability');AV=await r.json();if(!AV.first_date){$('#avail').innerHTML='No Claude Code session logs found in <code>~/.claude/projects/</code>. Use Claude Code first, then come back.';return}const n=AV.active_dates.length;const span=Math.round((new Date(AV.last_date)-new Date(AV.first_date))/864e5)+1;$('#avail').innerHTML=`Your logs cover <strong>${AV.first_date}</strong> to <strong>${AV.last_date}</strong> (${n} active days across ${span} calendar days, ${AV.total_files} session files). Use the date picker to filter within this range.`;$('#df').min=AV.first_date;$('#df').max=AV.last_date;$('#dt').min=AV.first_date;$('#dt').max=AV.last_date;$('#dt').value=AV.last_date;preset(30)}catch(e){$('#avail').textContent='Error: '+e.message}}
function preset(days){document.querySelectorAll('.bg').forEach(b=>b.classList.remove('active'));if(event&&event.target&&event.target.classList)event.target.classList.add('active');if(!AV||!AV.first_date)return;if(days===0){$('#df').value=AV.first_date;$('#dt').value=AV.last_date}else{const d=new Date(AV.last_date);d.setDate(d.getDate()-days);const m=new Date(AV.first_date);$('#df').value=(d<m?AV.first_date:d.toISOString().slice(0,10));$('#dt').value=AV.last_date}run()}
async function run(){const from=$('#df').value,to=$('#dt').value;if(!from||!to)return;$('#status').textContent='Scanning...';$('#loading').classList.remove('hidden');$('#loading').textContent='Analyzing sessions...';$('#dash').classList.add('hidden');try{const r=await fetch(`/api/analyze?from=${from}&to=${to}`);const d=await r.json();if(d.error){$('#loading').textContent=d.error;$('#status').textContent='';return}render(d);$('#dash').classList.remove('hidden');$('#loading').classList.add('hidden');$('#status').textContent=d.summary.sessions+' sessions'}catch(e){$('#loading').textContent='Error: '+e.message;$('#status').textContent=''}}
function fmt(n){return n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':n.toString()}
function fC(n){return n>=1?'$'+n.toFixed(2):n>=.01?'$'+n.toFixed(3):'$'+n.toFixed(4)}
function pct(n){return(n*100).toFixed(1)+'%'}
function sc(s){return s>=75?'var(--green)':s>=55?'var(--blue)':s>=35?'var(--yellow)':s>=20?'var(--orange)':'var(--red)'}
function tb(s){return s>=75?'var(--green-dim)':s>=55?'var(--blue-dim)':s>=35?'var(--yellow-dim)':'var(--orange-dim)'}
function rR(arr){if(!arr||!arr.length)return'';let h='<div class="srecs">';for(const r of arr)h+=`<div class="rec rec-${r.type}"><h4>${r.title}</h4><p>${r.body}</p></div>`;return h+'</div>'}
function render(d){
  const s=d.summary,e=d.efficiency,S=d.scores,R=d.recs||{},sh=d.session_health||{};
  let h='';

  // ── Score Section ──
  h+=`<div class="score-section">
    <div class="score-card"><div class="score-num" style="color:${sc(S.usage)}">${S.usage}</div><div class="score-label">Usage</div><div class="score-sub" style="color:${sc(S.usage)}">${S.usage_label}</div><div class="score-subdesc">${S.usage_desc}</div></div>
    <div class="score-op">+</div>
    <div class="score-card"><div class="score-num" style="color:${sc(S.efficiency)}">${S.efficiency}</div><div class="score-label">Efficiency</div><div class="score-sub" style="color:${sc(S.efficiency)}">${S.eff_label}</div><div class="score-subdesc">${S.eff_desc}</div></div>
    <div class="score-op">=</div>
    <div class="score-card main"><div class="score-num big" style="color:${sc(S.composite)}">${S.composite}</div><div class="score-label">Overall Score</div><div class="score-sub" style="color:${sc(S.composite)}">${S.tier}</div><div class="score-subdesc">${S.description}</div></div>
  </div>`;

  // ── Summary Cards ──
  h+=`<div class="cards"><div class="card"><div class="v">${fC(s.total_cost)}</div><div class="l">Estimated Cost</div><div class="s">${fC(e.projected_monthly)}/mo projected</div></div><div class="card"><div class="v">${s.sessions}</div><div class="l">Sessions</div><div class="s">${e.avg_duration_min.toFixed(0)} min avg</div></div><div class="card"><div class="v">${fmt(s.total_tokens)}</div><div class="l">Total Tokens</div><div class="s">${fmt(s.tokens_output)} output</div></div><div class="card"><div class="v">${s.total_user_msgs+s.total_asst_msgs}</div><div class="l">Messages</div><div class="s">${s.total_tool_calls} tool calls</div></div></div>`;

  // ── Where Your Money Goes ──
  const cb=d.cost_breakdown||{};
  const cbTotal=(cb.input||0)+(cb.output||0)+(cb.cache_read||0)+(cb.cache_write||0);
  if(cbTotal>0){
    h+='<div class="panel" style="margin-bottom:20px"><h3>Where Your Money Goes</h3><p class="panel-sub">Cost breakdown by token type</p>';
    const pI=(cb.input/cbTotal)*100,pO=(cb.output/cbTotal)*100,pCR=(cb.cache_read/cbTotal)*100,pCW=(cb.cache_write/cbTotal)*100;
    h+=`<div style="display:flex;height:32px;border-radius:6px;overflow:hidden;margin-bottom:16px">`;
    if(pCR>1)h+=`<div style="width:${pCR}%;background:#3b82f6;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Cache Reads: ${fC(cb.cache_read)}">${pCR>8?'Cache Read':''}</div>`;
    if(pCW>1)h+=`<div style="width:${pCW}%;background:#f97316;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Cache Writes: ${fC(cb.cache_write)}">${pCW>8?'Cache Write':''}</div>`;
    if(pO>1)h+=`<div style="width:${pO}%;background:#16a34a;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Output: ${fC(cb.output)}">${pO>5?'Output':''}</div>`;
    if(pI>1)h+=`<div style="width:${pI}%;background:#6366f1;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Input: ${fC(cb.input)}">${pI>5?'Input':''}</div>`;
    h+=`</div>`;
    h+=`<div style="display:flex;gap:24px;flex-wrap:wrap">`;
    h+=costLegend('#3b82f6','Cache Reads',cb.cache_read,pCR,'Re-reading your conversation history each turn');
    h+=costLegend('#f97316','Cache Writes',cb.cache_write,pCW,'Loading new files, tool results, or context');
    h+=costLegend('#16a34a','Output',cb.output,pO,"Claude's responses to you");
    h+=costLegend('#6366f1','Input',cb.input,pI,'Your prompts and instructions');
    h+='</div>';
    // Context window explanation
    const cachePct=pCR+pCW;
    if(cachePct>70){
      h+=`<div class="rec rec-info" style="margin-top:14px"><h4>Why is ${cachePct.toFixed(0)}% of your cost in cache?</h4><p>Every time you send a message, Claude re-reads your <strong>entire conversation history</strong> — every prior message, every file it read, every tool result. With the 1M context window model, sessions can grow very long, and Claude reprocesses all of that context on <em>every single turn</em>.</p><p style="margin-top:6px">Early in a session, this is cheap (small context). But by message 100+, Claude may be re-reading 500K+ tokens of history each turn. That's where your cost accumulates — not from Claude's responses (only ${pO.toFixed(0)}% of cost), but from re-reading the conversation over and over.</p><p style="margin-top:6px"><strong>What you can do:</strong> Start fresh sessions when switching tasks. Use <code>/clear</code> to reset context mid-session. The 1M window is powerful for deep work, but leaving stale context loaded means you're paying to re-read things Claude no longer needs.</p></div>`;
    }
    h+='</div>';
  }

  // ── By Project ──
  const proj=d.projects||[];
  if(proj.length>=1){
    h+='<div class="panel" style="margin-bottom:20px"><h3>By Project</h3><p class="panel-sub">'+
      (proj.length>1?'Where you\'re spending across different projects':'All sessions are from a single working directory')+'</p>';
    // Cost bar chart (only if multiple projects)
    if(proj.length>1){
      const mxC=proj[0].cost;
      h+='<div style="margin-bottom:16px">';
      for(const p of proj){
        const w=mxC>0?Math.max((p.cost/mxC)*100,3):0;
        h+=`<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">`;
        h+=`<div style="width:160px;font-size:12px;text-align:right;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.name}">${p.name}</div>`;
        h+=`<div style="flex:1;height:24px;background:var(--s3);border-radius:4px;overflow:hidden">`;
        h+=`<div style="height:100%;width:${w}%;background:var(--accent);border-radius:4px;display:flex;align-items:center;padding-left:8px;font-size:10px;font-weight:600;color:#fff;min-width:fit-content">${w>15?fC(p.cost):''}</div>`;
        h+=`</div>`;
        h+=`<div style="width:55px;font-size:12px;font-weight:600;text-align:right">${fC(p.cost)}</div>`;
        h+=`<div style="width:45px;font-size:10px;color:var(--dim);text-align:right">${pct(p.cost_pct)}</div>`;
        h+=`</div>`;
      }
      h+='</div>';
    }
    // Detail table
    h+='<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:11px">';
    h+='<thead><tr style="border-bottom:2px solid var(--border);text-align:left">';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600">Project</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Cost</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Sessions</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Messages</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Reads</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Writes</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Ext Calls</th>';
    h+='<th style="padding:6px 8px;color:var(--muted);font-weight:600;text-align:right">Efficiency</th>';
    h+='</tr></thead><tbody>';
    for(const p of proj){
      const ec=sc(p.efficiency);
      h+=`<tr style="border-bottom:1px solid var(--border)">`;
      h+=`<td style="padding:8px;font-weight:500" title="${p.name}">${p.name}</td>`;
      h+=`<td style="padding:8px;text-align:right;font-weight:600">${fC(p.cost)}</td>`;
      h+=`<td style="padding:8px;text-align:right">${p.sessions}</td>`;
      h+=`<td style="padding:8px;text-align:right">${p.messages}</td>`;
      h+=`<td style="padding:8px;text-align:right">${p.reads}</td>`;
      h+=`<td style="padding:8px;text-align:right">${p.writes}</td>`;
      h+=`<td style="padding:8px;text-align:right">${p.mcp_calls}</td>`;
      h+=`<td style="padding:8px;text-align:right"><span style="color:${ec};font-weight:600">${p.efficiency}</span></td>`;
      h+=`</tr>`;
    }
    h+='</tbody></table></div>';
    if(proj.length===1){h+=`<p style="font-size:11px;color:var(--dim);margin-top:10px;line-height:1.5">Tip: Launch Claude Code from specific project directories (<code>cd ~/Projects/my-app && claude</code>) to get per-project cost tracking. Right now all sessions share a single working directory.</p>`}
    h+='</div>';
  }

  // ── What's Working & What to Improve (two columns) ──
  const allRecs=[...(R.efficiency||[]),...(R.models||[]),...(R.health||[]),...(R.tools||[]),...(R.daily||[])];
  const good=allRecs.filter(r=>r.type==='good');
  const improve=allRecs.filter(r=>r.type==='warn'||r.type==='info');

  h+='<div class="cols">';
  h+='<div class="panel"><h3>What You\'re Doing Well</h3><p class="panel-sub">Keep these habits</p>';
  if(good.length){for(const r of good)h+=`<div class="rec rec-good"><h4>${r.title}</h4><p>${r.body}</p></div>`}
  else h+='<p style="font-size:12px;color:var(--muted)">No strong signals yet — use Claude more to build a pattern.</p>';
  h+='</div>';

  h+='<div class="panel"><h3>Where You Can Improve</h3><p class="panel-sub">Actionable ways to be more efficient</p>';
  if(improve.length){for(const r of improve)h+=`<div class="rec rec-${r.type}"><h4>${r.title}</h4><p>${r.body}</p></div>`}
  else h+='<p style="font-size:12px;color:var(--muted)">No issues detected — your usage looks efficient.</p>';
  h+='</div></div>';

  // ── How You Use Claude (two columns: Activity Breakdown + Cost by Service) ──
  h+='<div class="cols">';

  // Left: Activity breakdown as category summary bars
  h+='<div class="panel"><h3>Activity Breakdown</h3><p class="panel-sub">What Claude spends its time doing</p>';
  if(d.tools.length){
    const cats={explore:0,create:0,mcp:0,other:0};
    const exploreNames=['Read','Grep','Glob','Bash','WebSearch','WebFetch'];
    const createNames=['Edit','Write','NotebookEdit'];
    for(const t of d.tools){
      if(exploreNames.includes(t.name))cats.explore+=t.count;
      else if(createNames.includes(t.name))cats.create+=t.count;
      else if(t.name.includes('::'))cats.mcp+=t.count;
      else cats.other+=t.count;
    }
    const total=cats.explore+cats.create+cats.mcp+cats.other;
    // Stacked bar for categories
    const pe=(cats.explore/total)*100,pc=(cats.create/total)*100,pm=(cats.mcp/total)*100,po=(cats.other/total)*100;
    h+=`<div style="display:flex;height:28px;border-radius:6px;overflow:hidden;margin-bottom:16px">`;
    if(pe>1)h+=`<div style="width:${pe}%;background:var(--blue);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${pe>10?'Reading & Searching':''}</div>`;
    if(pc>1)h+=`<div style="width:${pc}%;background:var(--green);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${pc>10?'Writing & Editing':''}</div>`;
    if(pm>1)h+=`<div style="width:${pm}%;background:var(--orange);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${pm>8?'External Services':''}</div>`;
    if(po>1)h+=`<div style="width:${po}%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${po>8?'Other':''}</div>`;
    h+=`</div>`;
    h+=`<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:14px">`;
    h+=actLegend('var(--blue)','Reading & Searching',cats.explore,pe,'Browsing files, searching code, running commands');
    h+=actLegend('var(--green)','Writing & Editing',cats.create,pc,'Creating and modifying files');
    h+=actLegend('var(--orange)','External Services',cats.mcp,pm,'Slack, Snowflake, Google Docs, Figma, etc.');
    h+=actLegend('var(--accent)','Other',cats.other,po,'Task management, planning, subagents');
    h+=`</div>`;
    // Read:Write insight
    const rwR=cats.explore>0&&cats.create>0?(cats.explore/cats.create).toFixed(1):'N/A';
    h+=`<p style="font-size:11px;color:var(--muted);line-height:1.5">For every file you edit, Claude reads about <strong>${rwR}</strong> files first. ${cats.create>0&&cats.explore/cats.create<=4?'This is a healthy balance — reading before writing.':'A high ratio may mean a lot of exploration without producing output.'}</p>`;
  }
  h+='</div>';

  // Right: Model mix + Heaviest external services
  h+='<div class="panel"><h3>Cost by Model & Service</h3><p class="panel-sub">Which models and integrations cost the most</p>';
  if(d.models.length){
    h+='<div style="margin-bottom:6px;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Model</div>';
    const mx=d.models[0].tokens;
    for(const m of d.models){
      if(!m.tokens)continue;
      let nm=m.model.replace('claude-','').replace(/-20\d{6}$/,'');
      // Make model names friendlier
      if(nm.includes('opus'))nm='Opus (most capable, $5/$25 per MTok)';
      else if(nm.includes('sonnet'))nm='Sonnet (balanced, $3/$15 per MTok)';
      else if(nm.includes('haiku'))nm='Haiku (fastest, $1/$5 per MTok)';
      const c=m.model.includes('opus')?'var(--orange)':m.model.includes('haiku')?'var(--green)':'var(--blue)';
      h+=`<div class="br"><div class="bl" title="${m.model}" style="width:140px;text-align:left">${nm}</div><div class="bt"><div class="bf" style="width:${(m.tokens/mx)*100}%;background:${c}">${pct(m.pct)}</div></div><div class="bv" style="width:90px">${fC(m.cost)}</div></div>`;
    }
  }
  if(sh.mcp_heavy_tools&&sh.mcp_heavy_tools.length){
    h+='<div style="margin-top:18px;margin-bottom:6px;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Heaviest External Services</div>';
    h+='<p style="font-size:11px;color:var(--dim);margin-bottom:8px">Estimated tokens consumed by data returned from each service</p>';
    const mxMcp=sh.mcp_heavy_tools[0][1];
    // Map technical names to friendly names
    const friendly={'playwright::browser_take_screenshot':'Screenshots (Playwright)','granola::get_meeting_transcript':'Meeting Transcripts (Granola)','google-workspace::readGoogleDoc':'Google Docs','figma::get_screenshot':'Figma Screenshots','snowflake::run_snowflake_query':'Snowflake Queries','snowflake::list_objects':'Snowflake Schema','slack::slack_read_thread':'Slack Threads','slack::slack_search_public':'Slack Search','glean_default::read_document':'Glean Documents','glean_default::search':'Glean Search'};
    for(const [name,tok] of sh.mcp_heavy_tools){
      const display=friendly[name]||name;
      const label=tok>=1e3?((tok/1e3).toFixed(0)+'K'):String(tok);
      const estCost=tok*6.25/1e6;
      const barW=Math.max((tok/mxMcp)*100,8);
      h+=`<div class="br"><div class="bl" title="${name}" style="width:140px;text-align:left">${display}</div><div class="bt"><div class="bf" style="width:${barW}%;background:var(--orange)">${barW>30?label:''}</div></div><div class="bv" style="width:90px">${label} ~${fC(estCost)}</div></div>`;
    }
  }
  h+='</div></div>';

  // ── Daily Cost ──
  h+='<div class="panel" style="margin-bottom:20px"><h3>Daily Spend</h3><p class="panel-sub">How much you spent each day</p>';
  if(d.daily.length){
    const mx=Math.max(...d.daily.map(x=>x.cost));
    const show=d.daily.length>14?d.daily.slice(-14):d.daily;
    if(d.daily.length>14)h+=`<p style="font-size:11px;color:var(--dim);margin-bottom:8px">Showing last 14 days of ${d.daily.length}</p>`;
    for(const day of show){const lb=day.date.slice(5);const w=mx>0?(day.cost/mx)*100:0;const c=day.cost>100?'var(--orange)':day.cost>30?'var(--blue)':'var(--green)';h+=`<div class="br"><div class="bl">${lb}</div><div class="bt"><div class="bf" style="width:${w}%;background:${c}">${day.cost>=1?fC(day.cost):''}</div></div><div class="bv">${day.sessions}s ${day.msgs}m</div></div>`}
  }
  h+=rR(R.daily)+'</div>';

  // ── Session Health ──
  const healthRecs=(R.health||[]).filter(r=>!['Vague Prompts','Specific Prompts','Frequent Agent Loops','Heavy Subagent Usage'].includes(r.title));
  if(healthRecs.length){
    h+='<div class="panel" style="margin-bottom:20px"><h3>Session Health</h3><p class="panel-sub">Potential waste patterns detected</p>';
    h+=`<div style="display:flex;gap:24px;margin-bottom:14px;flex-wrap:wrap">`;
    h+=miniStat('Clean Sessions',sh.clean_pct+'%',sh.clean_pct>=90?'var(--green)':sh.clean_pct>=70?'var(--blue)':'var(--yellow)');
    h+=miniStat('Flagged',sh.flagged_count+' of '+s.sessions,sh.flagged_count<=2?'var(--green)':sh.flagged_count<=5?'var(--blue)':'var(--yellow)');
    h+=miniStat('Interrupts',String(sh.total_interrupts),sh.total_interrupts<=5?'var(--green)':sh.total_interrupts<=15?'var(--blue)':'var(--yellow)');
    h+=`</div>`;
    h+=rR(healthRecs)+'</div>';
  }

  // ── Trend ──
  if(d.trend){
    h+='<div class="panel" style="margin-bottom:20px"><h3>Trend vs. Prior Period</h3><p class="panel-sub">How your usage changed compared to the same-length prior window</p>';
    h+=tR('Tokens/msg',d.trend.prev_tokens_per_msg,d.trend.curr_tokens_per_msg,true,v=>v.toFixed(0));
    h+=tR('Cache rate',d.trend.prev_cache_rate,d.trend.curr_cache_rate,false,v=>pct(v));
    h+=tR('Cost/session',d.trend.prev_cost_per_session,d.trend.curr_cost_per_session,true,v=>fC(v));
    h+=tR('Total cost',d.trend.prev_total_cost,d.trend.curr_total_cost,true,v=>fC(v));
    h+=tR('Sessions',d.trend.prev_sessions,d.trend.curr_sessions,false,v=>v.toString());
    h+='</div>';
  }

  $('#dash').innerHTML=h;
}

function costLegend(color,label,cost,pct,desc){return`<div style="display:flex;align-items:flex-start;gap:6px"><div style="width:10px;height:10px;border-radius:2px;background:${color};margin-top:3px;flex-shrink:0"></div><div><div style="font-size:12px"><span style="color:var(--muted)">${label}</span> <strong>${fC(cost)}</strong> <span style="font-size:11px;color:var(--dim)">(${pct.toFixed(0)}%)</span></div><div style="font-size:10px;color:var(--dim)">${desc}</div></div></div>`}
function actLegend(color,label,count,pct,desc){return`<div style="display:flex;align-items:flex-start;gap:6px"><div style="width:10px;height:10px;border-radius:2px;background:${color};margin-top:3px;flex-shrink:0"></div><div><div style="font-size:12px"><span style="color:var(--muted)">${label}</span> <strong>${count}</strong> <span style="font-size:11px;color:var(--dim)">(${pct.toFixed(0)}%)</span></div><div style="font-size:10px;color:var(--dim)">${desc}</div></div></div>`}
function bar(name,count,mx,color){return`<div class="br"><div class="bl" title="${name}">${name}</div><div class="bt"><div class="bf" style="width:${(count/mx)*100}%;background:${color}">${count}</div></div><div class="bv"></div></div>`}
function miniStat(label,val,color){return`<div style="text-align:center"><div style="font-size:20px;font-weight:700;color:${color}">${val}</div><div style="font-size:10px;color:var(--muted);margin-top:2px">${label}</div></div>`}
function gauge(l,v,mx,disp,c){const w=Math.min((v/mx)*100,100);return`<div class="gr"><div class="gl">${l}</div><div class="gt"><div class="gf" style="width:${w}%;background:${c}"></div></div><div class="gv" style="color:${c}">${disp}</div></div>`}
function tR(l,p,c,lib,fn){if(p===0)return`<div class="tr"><div class="tm">${l}</div><div class="tv">${fn(c)}</div><span class="ta ta-s">new</span></div>`;const pc=((c-p)/p)*100;let cls,txt;if(Math.abs(pc)<5){cls='ta-s';txt=`~${Math.abs(pc).toFixed(0)}%`}else if(pc>0){cls=lib?'ta-ub':'ta-ug';txt=`+${pc.toFixed(0)}%`}else{cls=lib?'ta-dg':'ta-db';txt=`${pc.toFixed(0)}%`}return`<div class="tr"><div class="tm">${l}</div><div class="tv">${fn(p)} &rarr; ${fn(c)}</div><span class="ta ${cls}">${txt}</span></div>`}

init();
</script>
</body>
</html>"""


# ── Server ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/availability":
            result = scan_availability()
            self._json(result)
        elif parsed.path == "/api/analyze":
            qs = parse_qs(parsed.query)
            df = datetime.strptime(qs.get("from", [_today()])[0], "%Y-%m-%d").date()
            dt = datetime.strptime(qs.get("to", [_today()])[0], "%Y-%m-%d").date()
            self._json(analyze(df, dt))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *a):
        pass


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def main():
    parser = argparse.ArgumentParser(description="Claude Code Efficiency Analyzer")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Claude Code Efficiency Analyzer running at {url}")
    print("Press Ctrl+C to stop.\n")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
