#!/usr/bin/env python3
"""
Claude Code Efficiency Analyzer
Reads your local Claude Code session logs and shows you how you actually use the tool —
spending, token distribution, where context bloats, and where to focus.

Usage:
  python3 analyzer.py                  # opens browser on port 8741
  python3 analyzer.py --port 9000      # custom port
  python3 analyzer.py --no-open        # don't auto-open browser
  python3 analyzer.py --export OUT.json # write report JSON for the last 30 days and exit
  python3 analyzer.py --days 30        # period for --export and dashboard window (default 30)
  python3 analyzer.py --demo           # synthetic preview (solo-dev persona, default)
  python3 analyzer.py --demo pm        # synthetic preview (PM persona)
  python3 analyzer.py --demo writer    # synthetic preview (writer/researcher persona)
"""

import argparse
import glob
import json
import os
import random
import re
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Pricing (USD per million tokens, May 2026) ───────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# 5m cache write = 1.25x base input. 1h cache write = 2x base input. Cache read = 0.1x base input.
# 1M context window: standard pricing for Opus 4.6+ / Sonnet 4.6+.
PRICING = {
    "claude-opus-4-7":            {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-6":            {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-5-20250918":   {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-sonnet-4-6":          {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "claude-haiku-4-5-20251001":  {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_write_5m": 1.25, "cache_write_1h": 2.0},
}
FALLBACK_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6.0}

WEB_SEARCH_PER_REQUEST = 10.0 / 1000  # $10 per 1,000 searches

READ_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
EXPLORE_TOOLS = {"Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"}

FRIENDLY_MCP = {
    "playwright::browser_take_screenshot": "Browser screenshots",
    "playwright::browser_navigate": "Browser navigation",
    "playwright::browser_snapshot": "Browser snapshots",
    "granola::get_meeting_transcript": "Meeting transcripts",
    "granola::get_meetings": "Meeting notes",
    "granola::list_meetings": "Meeting list",
    "google-workspace::readGoogleDoc": "Google Doc reads",
    "google-workspace::editGoogleDoc": "Google Doc edits",
    "google-workspace::writeSpreadsheet": "Sheets writes",
    "google-workspace::readSpreadsheet": "Sheets reads",
    "google-workspace::listCalendarEvents": "Calendar lookups",
    "google-workspace::listRecentFiles": "Drive listing",
    "google-workspace::searchDrive": "Drive search",
    "figma::get_screenshot": "Figma screenshots",
    "figma::get_design_context": "Figma designs",
    "figma::get_metadata": "Figma metadata",
    "snowflake::run_snowflake_query": "Snowflake queries",
    "snowflake::list_objects": "Snowflake schema",
    "snowflake::describe_object": "Snowflake describe",
    "slack::slack_read_thread": "Slack threads",
    "slack::slack_search_public": "Slack search",
    "slack::slack_search_public_and_private": "Slack search",
    "slack::slack_send_message": "Slack messages",
    "glean_default::read_document": "Glean documents",
    "glean_default::search": "Glean search",
}

FRIENDLY_BUILTIN = {
    "Read": "Read file",
    "Write": "Write file",
    "Edit": "Edit file",
    "Bash": "Shell command",
    "Grep": "Grep search",
    "Glob": "Glob match",
    "WebFetch": "Fetch URL",
    "WebSearch": "Web search",
    "Task": "Spawn subagent",
    "Skill": "Run skill",
    "TaskCreate": "Create task",
    "TaskUpdate": "Update task",
    "TaskList": "List tasks",
    "TaskGet": "Get task",
    "ExitPlanMode": "Exit plan mode",
    "NotebookEdit": "Edit notebook",
    "ToolSearch": "Tool lookup",
    "Agent": "Spawn subagent",
}


def friendly_tool_name(name):
    if not name:
        return "(text-only)"
    if name in FRIENDLY_BUILTIN:
        return FRIENDLY_BUILTIN[name]
    short = name.replace("mcp__", "").replace("__", "::")
    return FRIENDLY_MCP.get(short, short)


def friendly_model_name(model):
    if not model:
        return "Unknown"
    m = model.lower()
    if "opus-4-7" in m: return "Opus 4.7"
    if "opus-4-6" in m: return "Opus 4.6"
    if "opus" in m:    return "Opus"
    if "sonnet-4-6" in m: return "Sonnet 4.6"
    if "sonnet" in m:  return "Sonnet"
    if "haiku" in m:   return "Haiku"
    return model.replace("claude-", "").replace(re.search(r"-20\d{6}$", model).group() if re.search(r"-20\d{6}$", model) else "", "")


# ── Tier vocabulary (descriptive, not graded) ────────────────────────────────

USAGE_BANDS = [(75, "Heavy"), (50, "Steady"), (25, "Light"), (0, "Sampling")]
EFFICIENCY_BANDS = [(75, "High"), (50, "Solid"), (25, "Mixed"), (0, "Spotty")]
COMPOSITE_BANDS = [(85, "Power Use"), (65, "Heavy Use"), (45, "Steady Use"), (25, "Light Use"), (0, "Just Starting")]


def _band(score, bands):
    for threshold, label in bands:
        if score >= threshold:
            return label
    return bands[-1][1]


# ── Pricing lookups ──────────────────────────────────────────────────────────

def get_pricing(model):
    if not model:
        return FALLBACK_PRICING
    if model in PRICING:
        return PRICING[model]
    for key in sorted(PRICING.keys(), key=len, reverse=True):
        if key in model:
            return PRICING[key]
    ml = model.lower()
    if "opus" in ml:   return PRICING["claude-opus-4-7"]
    if "sonnet" in ml: return PRICING["claude-sonnet-4-6"]
    if "haiku" in ml:  return PRICING["claude-haiku-4-5-20251001"]
    return FALLBACK_PRICING


def cost_components(model, usage):
    p = get_pricing(model)
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cc_total = usage.get("cache_creation_input_tokens", 0) or 0

    cc = usage.get("cache_creation") or {}
    cc_5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
    cc_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    if cc_5m == 0 and cc_1h == 0 and cc_total > 0:
        cc_5m = cc_total

    server = usage.get("server_tool_use") or {}
    web_search_n = server.get("web_search_requests", 0) or 0

    return {
        "input":          (inp / 1e6) * p["input"],
        "output":         (out / 1e6) * p["output"],
        "cache_read":     (cr / 1e6) * p["cache_read"],
        "cache_write_5m": (cc_5m / 1e6) * p["cache_write_5m"],
        "cache_write_1h": (cc_1h / 1e6) * p["cache_write_1h"],
        "web_search":     web_search_n * WEB_SEARCH_PER_REQUEST,
        "_tokens": {
            "input": inp, "output": out, "cache_read": cr,
            "cache_write_5m": cc_5m, "cache_write_1h": cc_1h,
            "web_search_requests": web_search_n,
        },
    }


def total_cost(comp):
    return comp["input"] + comp["output"] + comp["cache_read"] + comp["cache_write_5m"] + comp["cache_write_1h"] + comp["web_search"]


def dominant_cost_driver(comp):
    """Which of {cache_read, cache_write, output, input, web_search} dominated this turn?"""
    cw = comp["cache_write_5m"] + comp["cache_write_1h"]
    parts = [
        ("cache_read", comp["cache_read"]),
        ("cache_write", cw),
        ("output", comp["output"]),
        ("input", comp["input"]),
        ("web_search", comp["web_search"]),
    ]
    parts.sort(key=lambda x: -x[1])
    return parts[0][0] if parts[0][1] > 0 else "input"


# ── Project discovery (memoized) ──────────────────────────────────────────────

_project_name_cache = {}


def find_project_dirs():
    home = os.path.expanduser("~")
    base = os.path.join(home, ".claude", "projects")
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, d) for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]


def project_name_from_dir(dirpath):
    if dirpath in _project_name_cache:
        return _project_name_cache[dirpath]
    cwd = None
    for fpath in glob.glob(os.path.join(dirpath, "*.jsonl"))[:3]:
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
    name = _format_project_name(cwd, dirpath)
    _project_name_cache[dirpath] = name
    return name


def _format_project_name(cwd, dirpath):
    home = os.path.expanduser("~")
    if not cwd:
        dirname = os.path.basename(dirpath).lstrip("-")
        home_encoded = home.lstrip("/").replace("/", "-").replace(".", "-")
        if dirname == home_encoded:
            return "~ (home)"
        if dirname.startswith(home_encoded + "-"):
            return "~/" + dirname[len(home_encoded) + 1:]
        return dirname
    if cwd.rstrip("/") == home.rstrip("/"):
        return "~ (home)"
    rel = cwd[len(home) + 1:] if cwd.startswith(home + "/") else cwd
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return "~ (home)"
    generic = {"Desktop", "Documents", "Projects", "repos", "src", "code", "dev", "work", "workspace"}
    for i, p in enumerate(parts):
        if p not in generic:
            if i > 0 and parts[i - 1] in generic:
                return "/".join(parts[i - 1:])
            return "/".join(parts[i:])
    return parts[-1]


# ── Session label + categorization ───────────────────────────────────────────
# Heuristics intentionally cover a wide range of stacks: data warehouses
# (Snowflake/BigQuery/Postgres), PM tools (Notion/Linear/Jira/Asana), design
# (Figma/Sketch), team comms (Slack/Discord), and personal-context signals
# (side project, learning, content creation). Extend as new MCP servers ship.

CATEGORY_KEYWORDS = {
    "Data Analysis": ["sql", "query", "metric", "snowflake", "bigquery", "postgres", "mysql",
                       "mongodb", "redshift", "duckdb", "dbt", "looker", "tableau", "metabase",
                       "table_", "select ", "group by", "join ", "data analysis", "pull data",
                       "jupyter", "notebook", "pandas", "ipynb", "dataframe"],
    "PM Work": ["meeting", "digest", "morning sync", "weekly", "stand-up", "standup", "prep ",
                "leadership", "stakeholder", "update doc", "brief", "prd ", "spec doc",
                "roadmap", "okr", "kpi", "release notes"],
    "Design Work": ["figma", "sketch", "framer", "penpot", "miro", "whimsical", "excalidraw",
                     "design", "prototype", "mockup", "wireframe", "ui ", "ux "],
    "Debugging": ["debug", "fix ", "broken", "error", "stack trace", "why is", "why does",
                   "investigate", "stacktrace", "exception", "crash"],
    "Writing": ["write a", "draft", "compose", "rewrite", "edit doc", "blog", "post about",
                 "newsletter", "essay", "tweet", "linkedin post", "thread", "article"],
    "Research": ["research", "look into", "what is", "explain how", "compare", "summarize",
                  "deep dive", "literature", "competitive"],
    "Learning": ["learn ", "tutorial", "walk me through", "teach me", "how do i", "how do you",
                  "what's the difference", "what is the difference", "course", "study"],
    "Personal Project": ["my side project", "side project", "personal site", "portfolio",
                          "hobby", "weekend project", "for fun", "playing around", "tinker"],
    "Coding": ["refactor", "implement", "build a", "add a feature", "rename ", "test ",
                "lint ", "code review", "ship a", "scaffold"],
}

# MCP server name fragments that map to a category. Lowercased substring match.
# Add liberally as new servers appear.
CATEGORY_MCP_PATTERNS = {
    "Data Analysis": ["snowflake", "bigquery", "postgres", "mysql", "redshift", "duckdb",
                       "databricks", "dbt", "metabase", "supabase"],
    "Design Work": ["figma", "sketch", "framer", "penpot", "miro"],
    "PM Work": ["notion", "linear", "jira", "asana", "clickup", "monday", "trello",
                 "confluence", "granola", "fireflies", "fathom"],
    "Communication": ["slack", "discord", "teams", "telegram", "twilio"],
    "Knowledge": ["glean", "google-workspace", "google_workspace", "office365", "dropbox"],
}


def _count_mcp_matches(tools, fragments):
    return sum(c for t, c in tools.items() if any(f in t.lower() for f in fragments))


def categorize_session(session, first_prompt):
    text = (first_prompt or "").lower()[:2000]
    tools = session["tool_calls"]
    write_n = session["writes"]
    read_n = session["reads"]
    mcp_n = session["mcp_calls"]

    # Strong MCP-tool signals fire first — a specialized server dominating
    # the session is unambiguous (e.g. Snowflake = data, Figma = design).
    data_n = _count_mcp_matches(tools, CATEGORY_MCP_PATTERNS["Data Analysis"])
    design_n = _count_mcp_matches(tools, CATEGORY_MCP_PATTERNS["Design Work"])
    pm_n = _count_mcp_matches(tools, CATEGORY_MCP_PATTERNS["PM Work"])
    comm_n = _count_mcp_matches(tools, CATEGORY_MCP_PATTERNS["Communication"])
    knowledge_n = _count_mcp_matches(tools, CATEGORY_MCP_PATTERNS["Knowledge"])

    if data_n >= 3:
        return "Data Analysis"
    if design_n >= 2:
        return "Design Work"
    if pm_n >= 2 or comm_n >= 5 or knowledge_n >= 5:
        return "PM Work"

    # Intent (the user's first prompt) beats shape (tool counts). A session
    # that says "draft the newsletter" or "research X" is Writing/Research
    # even if Claude wrote 10 markdown files in the process.
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return cat

    # Shape-based fallbacks for sessions with no clear intent keyword.
    if write_n >= 5 and (read_n / max(write_n, 1)) <= 5 and mcp_n < 5:
        return "Coding"
    if knowledge_n >= 2 or (read_n >= 5 and write_n == 0):
        return "Research"
    if write_n >= 3:
        return "Coding"
    return "Other"


def extract_session_label(first_prompt, session_id):
    if not first_prompt:
        return f"session {session_id[:8] if session_id else '?'}"
    label = re.sub(r"\s+", " ", first_prompt).strip()
    # Strip common Claude Code system-injected prefixes.
    for prefix in ["<system-reminder>", "<command-name>", "[Request interrupted"]:
        if label.startswith(prefix):
            label = label.split(">", 1)[-1].strip() if ">" in label else label
    if len(label) > 80:
        label = label[:77].rstrip() + "…"
    return label or f"session {session_id[:8] if session_id else '?'}"


# ── Single-pass parsing ──────────────────────────────────────────────────────

def _new_session():
    return {
        "session_id": None,
        "project_dir": None,
        "label": None,
        "category": "Other",
        "first_prompt": None,
        "messages_user": 0, "messages_assistant": 0,
        "tokens": defaultdict(int),
        "cost": defaultdict(float),
        "cost_total": 0.0,
        "tool_calls": defaultdict(int),
        "by_model_tokens": defaultdict(int),
        "by_model_cost": defaultdict(float),
        "by_date": defaultdict(lambda: {"tokens": 0, "cost": 0.0, "msgs": 0}),
        "timestamps": [],
        "reads": 0, "writes": 0, "mcp_calls": 0, "interrupts": 0,
        "mcp_tools": defaultdict(int),
        "mcp_tokens": 0,
        "mcp_tool_tokens": defaultdict(int),
        "read_signatures": defaultdict(int),
        "max_agent_streak": 0, "_cur_streak": 0,
        "short_prompts": 0, "detailed_prompts": 0, "total_prompts": 0,
        "subagent_spawns": 0, "skill_invocations": 0,
        "skills_used": defaultdict(int),
        "plan_mode_uses": 0,
        "truncated_turns": 0,
        "sidechain_cost": 0.0, "sidechain_tokens": 0,
        "per_turn_cache_read": [],
        "per_turn_cost": [],
        "_assistant_idx": 0,
        "_pending_tool_chars": [],
        "duration_min": 0,
        "opus_total_tokens": 0,
        "opus_trivial_tokens": 0,
    }


def _classify_assistant_content(content_blocks):
    tools = []
    out_chars = 0
    for blk in content_blocks or []:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "tool_use":
            tools.append(blk.get("name", "unknown"))
        elif blk.get("type") == "text":
            out_chars += len(blk.get("text", ""))
    return tools, out_chars


def _parse_user_message(rec, sess):
    sess["messages_user"] += 1
    sess["_cur_streak"] = 0
    msg = rec.get("message", {}) or {}
    content = msg.get("content", "")
    prompt_len = 0
    prompt_text = ""

    if isinstance(content, str):
        prompt_len = len(content)
        prompt_text = content
        if "interrupted" in content.lower():
            sess["interrupts"] += 1
    elif isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                t = blk.get("text", "")
                prompt_len += len(t)
                prompt_text += t + " "
                if "interrupted" in t.lower():
                    sess["interrupts"] += 1
            elif btype == "tool_result":
                rc = blk.get("content", "")
                if isinstance(rc, str):
                    chars = len(rc)
                elif isinstance(rc, list):
                    chars = sum(len(str(b)) for b in rc)
                else:
                    chars = len(str(rc))
                sess["_pending_tool_chars"].append((blk.get("tool_use_id", ""), chars))

    if prompt_len > 0:
        sess["total_prompts"] += 1
        if prompt_len < 20:
            sess["short_prompts"] += 1
        elif prompt_len > 500:
            sess["detailed_prompts"] += 1

    # First substantive user prompt becomes the session label seed.
    if prompt_text.strip() and not sess["first_prompt"] and not rec.get("isMeta"):
        # Skip system-injected stuff.
        clean = prompt_text.strip()
        if not clean.startswith("<") and not clean.startswith("[Request interrupted"):
            sess["first_prompt"] = clean


def _parse_assistant_message(rec, sess, recent_tool_uses):
    sess["messages_assistant"] += 1
    sess["_cur_streak"] += 1
    sess["max_agent_streak"] = max(sess["max_agent_streak"], sess["_cur_streak"])
    sess["_assistant_idx"] += 1
    turn_idx = sess["_assistant_idx"]

    msg = rec.get("message", {}) or {}
    usage = msg.get("usage", {}) or {}
    model = msg.get("model", "unknown")
    stop_reason = msg.get("stop_reason", "")

    if stop_reason == "max_tokens":
        sess["truncated_turns"] += 1

    comp = cost_components(model, usage)
    c = total_cost(comp)
    sess["cost_total"] += c
    for k in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "web_search"):
        sess["cost"][k] += comp[k]
    tk = comp["_tokens"]
    for k, v in tk.items():
        sess["tokens"][k] += v
    ttok = tk["input"] + tk["output"] + tk["cache_read"] + tk["cache_write_5m"] + tk["cache_write_1h"]
    sess["by_model_tokens"][model] += ttok
    sess["by_model_cost"][model] += c
    sess["per_turn_cache_read"].append((turn_idx, tk["cache_read"]))

    if rec.get("isSidechain"):
        sess["sidechain_cost"] += c
        sess["sidechain_tokens"] += ttok

    content = msg.get("content", []) or []
    tools_in_turn, out_chars = _classify_assistant_content(content)
    is_only_explore = bool(tools_in_turn) and all(t in EXPLORE_TOOLS for t in tools_in_turn)
    is_trivial_explore = is_only_explore and tk["output"] < 200
    if "opus" in (model or "").lower():
        sess["opus_total_tokens"] += ttok
        if is_trivial_explore:
            sess["opus_trivial_tokens"] += ttok

    top_tool = None
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "tool_use":
            name = blk.get("name", "unknown")
            sess["tool_calls"][name] += 1
            top_tool = top_tool or name
            if name in READ_TOOLS:
                sess["reads"] += 1
            elif name in WRITE_TOOLS:
                sess["writes"] += 1
            if "mcp__" in name:
                sess["mcp_calls"] += 1
                sess["mcp_tools"][name] += 1
                recent_tool_uses[blk.get("id", "")] = name
            if name == "Read":
                inp_args = blk.get("input") or {}
                if isinstance(inp_args, dict):
                    sig = (inp_args.get("file_path", ""),
                           inp_args.get("offset", None),
                           inp_args.get("limit", None))
                    if sig[0]:
                        sess["read_signatures"][sig] += 1
            if name in ("Task", "Agent"):
                sess["subagent_spawns"] += 1
            if name == "Skill":
                sess["skill_invocations"] += 1
                inp_args = blk.get("input") or {}
                skill = inp_args.get("skill", "unknown") if isinstance(inp_args, dict) else "unknown"
                sess["skills_used"][skill] += 1
            if name == "ExitPlanMode":
                sess["plan_mode_uses"] += 1

    sess["per_turn_cost"].append({
        "idx": turn_idx, "cost": c, "model": model,
        "tool": top_tool or "(text-only)",
        "ts": rec.get("timestamp", ""),
        "session_id": sess["session_id"],
        "cache_read": tk["cache_read"],
        "output": tk["output"],
        "cache_write": tk["cache_write_5m"] + tk["cache_write_1h"],
        "driver": dominant_cost_driver(comp),
    })

    pending = sess["_pending_tool_chars"]
    if pending:
        total_pending_chars = sum(c for _, c in pending) or 1
        new_cache = tk["cache_write_5m"] + tk["cache_write_1h"]
        attributable = int(new_cache * 0.85)
        for tool_id, chars in pending:
            tool_name = recent_tool_uses.get(tool_id)
            if not tool_name or "mcp__" not in tool_name:
                continue
            share = int(attributable * (chars / total_pending_chars))
            fallback = chars // 3
            est = max(share, fallback)
            sess["mcp_tokens"] += est
            sess["mcp_tool_tokens"][tool_name] += est
        sess["_pending_tool_chars"] = []

    return c


def _parse_record(rec, sess, recent_tool_uses):
    rtype = rec.get("type")
    ts_str = rec.get("timestamp")
    cur_day = None

    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            cur_day = str(ts.date())
            sess["timestamps"].append(ts)
            if not sess["session_id"]:
                sess["session_id"] = rec.get("sessionId")
        except (ValueError, TypeError):
            pass

    if rtype == "permission-mode" and rec.get("permissionMode") == "plan":
        sess["plan_mode_uses"] += 1
        return

    if rtype == "user":
        if cur_day:
            sess["by_date"][cur_day]["msgs"] += 1
        _parse_user_message(rec, sess)
    elif rtype == "assistant":
        c = _parse_assistant_message(rec, sess, recent_tool_uses)
        if cur_day:
            tk = rec.get("message", {}).get("usage", {}) or {}
            ttok = sum([tk.get("input_tokens", 0) or 0, tk.get("output_tokens", 0) or 0,
                        tk.get("cache_read_input_tokens", 0) or 0, tk.get("cache_creation_input_tokens", 0) or 0])
            sess["by_date"][cur_day]["tokens"] += ttok
            sess["by_date"][cur_day]["cost"] += c
            sess["by_date"][cur_day]["msgs"] += 1


def parse_session_file(filepath):
    sess = _new_session()
    recent_tool_uses = {}
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
                _parse_record(rec, sess, recent_tool_uses)
    except Exception:
        return None

    if sess["messages_assistant"] == 0:
        return None
    # Wall-clock span — useful for sorting "longest" sessions but misleading
    # if a tab was left idle for hours. Kept for "Best Moments" context only.
    if len(sess["timestamps"]) >= 2:
        sess["span_min"] = (max(sess["timestamps"]) - min(sess["timestamps"])).total_seconds() / 60
    else:
        sess["span_min"] = 0
    # Focused active time — sum of gaps between consecutive timestamps capped at
    # 5 minutes per gap. This filters out idle stretches (overnight, parallel tabs).
    sorted_ts = sorted(sess["timestamps"])
    focused = 0.0
    for a, b in zip(sorted_ts, sorted_ts[1:]):
        gap = (b - a).total_seconds() / 60
        if 0 < gap <= 5:
            focused += gap
    sess["duration_min"] = focused

    sess["redundant_reads"] = sum(max(0, c - 1) for c in sess["read_signatures"].values())
    sess["short_prompt_pct"] = sess["short_prompts"] / max(sess["total_prompts"], 1)
    cr_per_turn = [c for _, c in sess["per_turn_cache_read"]]
    sess["avg_cache_read_per_turn"] = sum(cr_per_turn) / len(cr_per_turn) if cr_per_turn else 0
    sess["max_cache_read_per_turn"] = max(cr_per_turn) if cr_per_turn else 0

    flags = []
    if sess["reads"] > 5 and sess["writes"] == 0:
        flags.append("bulk-read-no-output")
    if sess["mcp_calls"] > 20:
        flags.append("mcp-heavy")
    if sess["messages_user"] < 3 and sess["messages_assistant"] > 10:
        flags.append("low-interaction")
    if sess["interrupts"] > 2:
        flags.append("high-interrupts")
    if sess["max_agent_streak"] >= 8:
        flags.append("deep-agent-loop")
    if sess["redundant_reads"] > 5:
        flags.append("redundant-reads")
    if sess["avg_cache_read_per_turn"] > 300_000:
        flags.append("stale-context")
    if sess["truncated_turns"] > max(2, sess["messages_assistant"] * 0.05):
        flags.append("truncated-output")
    sess["flags"] = flags

    sess["label"] = extract_session_label(sess["first_prompt"], sess["session_id"])
    sess["category"] = categorize_session(sess, sess["first_prompt"])

    if sess["timestamps"]:
        sess["first_date"] = min(sess["timestamps"]).date()
        sess["last_date"] = max(sess["timestamps"]).date()
    else:
        sess["first_date"] = None
        sess["last_date"] = None

    sess.pop("_cur_streak", None)
    sess.pop("_pending_tool_chars", None)
    sess.pop("_assistant_idx", None)
    return sess


def load_all_sessions():
    dirs = find_project_dirs()
    sessions = []
    for d in dirs:
        for fpath in glob.glob(os.path.join(d, "*.jsonl")):
            s = parse_session_file(fpath)
            if s:
                s["project_dir"] = d
                s["is_subagent"] = False
                sessions.append(s)
        # Subagent invocations write their own jsonl under
        # <project>/<parent-session-uuid>/subagents/*.jsonl. Each contains
        # independent token usage — must be counted, not skipped.
        for fpath in glob.glob(os.path.join(d, "*", "subagents", "*.jsonl")):
            s = parse_session_file(fpath)
            if s:
                s["project_dir"] = d
                s["is_subagent"] = True
                sessions.append(s)
    return sessions


def slice_sessions(all_sessions, date_from, date_to):
    out = []
    for s in all_sessions:
        if not s.get("first_date") or not s.get("last_date"):
            continue
        if s["last_date"] < date_from or s["first_date"] > date_to:
            continue
        in_range_dates = [(d, dd) for d, dd in s["by_date"].items()
                          if date_from <= datetime.fromisoformat(d).date() <= date_to]
        if not in_range_dates:
            continue
        out.append((s, dict(in_range_dates)))
    return out


# ── Scoring ──────────────────────────────────────────────────────────────────

def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def score_usage(sliced, num_days):
    if num_days <= 0 or not sliced:
        return 0
    active_days = len({d for s, dates in sliced for d in dates.keys()})
    total_msgs = sum(s["messages_user"] + s["messages_assistant"] for s, _ in sliced)
    total_sessions = len(sliced)
    day_ratio = active_days / num_days
    day_score = clamp(day_ratio * 125)
    sess_per_day = total_sessions / max(active_days, 1)
    sess_score = 100 if sess_per_day >= 4 else 75 if sess_per_day >= 2 else 55 if sess_per_day >= 1 else 30
    msgs_per_day = total_msgs / max(active_days, 1)
    msg_score = (100 if msgs_per_day >= 100 else 80 if msgs_per_day >= 50 else 60 if msgs_per_day >= 20
                 else 35 if msgs_per_day >= 5 else 15)
    return clamp(round(day_score * 0.40 + sess_score * 0.30 + msg_score * 0.30))


def score_efficiency(sliced):
    if not sliced:
        return 0
    sessions = [s for s, _ in sliced]
    total_input = sum(s["tokens"]["input"] for s in sessions)
    total_output = sum(s["tokens"]["output"] for s in sessions)
    total_cr = sum(s["tokens"]["cache_read"] for s in sessions)
    total_cw = sum(s["tokens"]["cache_write_5m"] + s["tokens"]["cache_write_1h"] for s in sessions)
    total_asst = sum(s["messages_assistant"] for s in sessions)
    total_msgs = sum(s["messages_user"] + s["messages_assistant"] for s in sessions)

    cache_denom = total_cr + total_cw + total_input
    cache_rate = total_cr / cache_denom if cache_denom > 0 else 0
    cache_score = (100 if cache_rate >= 0.90 else 80 if cache_rate >= 0.80 else 60 if cache_rate >= 0.65
                   else 40 if cache_rate >= 0.50 else 20)

    mps = total_msgs / len(sessions)
    mps_score = (100 if mps >= 80 else 80 if mps >= 40 else 60 if mps >= 15
                 else 35 if mps >= 5 else 15)

    tpm = total_output / max(total_asst, 1)
    tpm_score = (100 if tpm <= 200 else 85 if tpm <= 500 else 65 if tpm <= 1500
                 else 45 if tpm <= 3000 else 25)

    opus_total = sum(s.get("opus_total_tokens", 0) for s in sessions)
    opus_trivial = sum(s.get("opus_trivial_tokens", 0) for s in sessions)
    if opus_total > 0:
        trivial_pct = opus_trivial / opus_total
        task_fit_score = (100 if trivial_pct <= 0.10 else 85 if trivial_pct <= 0.20
                          else 60 if trivial_pct <= 0.35 else 40 if trivial_pct <= 0.5 else 25)
    else:
        task_fit_score = 60

    total_reads = sum(s["reads"] for s in sessions)
    total_writes = sum(s["writes"] for s in sessions)
    if total_writes == 0 and total_reads == 0:
        rw_score = 50
    elif total_writes == 0:
        rw_score = 20
    else:
        ratio = total_reads / total_writes
        rw_score = 100 if ratio <= 4 else 70 if ratio <= 8 else 45 if ratio <= 15 else 25

    flagged = sum(1 for s in sessions if s.get("flags"))
    health_score = clamp(round((1 - flagged / len(sessions)) * 100))

    return clamp(round(
        cache_score * 0.25 + mps_score * 0.15 + tpm_score * 0.15
        + task_fit_score * 0.15 + rw_score * 0.15 + health_score * 0.15
    ))


def composite_score(usage, efficiency):
    return clamp(round(usage * 0.40 + efficiency * 0.40 + min(usage, efficiency) * 0.20))


# ── Availability ────────────────────────────────────────────────────────────

def read_cleanup_period():
    """Read cleanupPeriodDays from ~/.claude/settings.json. Default is 30."""
    path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(path) as f:
            settings = json.load(f)
        val = settings.get("cleanupPeriodDays")
        if isinstance(val, (int, float)) and val > 0:
            return {"days": int(val), "is_default": False}
    except Exception:
        pass
    return {"days": 30, "is_default": True}


def scan_availability(all_sessions):
    active_dates = set()
    for s in all_sessions:
        active_dates.update(s["by_date"].keys())
    if not active_dates:
        return {"first_date": None, "last_date": None, "active_dates": [], "total_files": 0,
                "retention": read_cleanup_period()}
    sorted_dates = sorted(active_dates)
    return {
        "first_date": sorted_dates[0], "last_date": sorted_dates[-1],
        "active_dates": sorted_dates, "total_files": len(all_sessions),
        "retention": read_cleanup_period(),
    }


# ── Top finding logic ────────────────────────────────────────────────────────

def compute_top_finding(sessions, totals):
    """Find the highest-impact concrete opportunity with $ savings estimate."""
    candidates = []

    # Stale-context savings.
    stale_cost = sum(s["cost_total"] for s in sessions if "stale-context" in s.get("flags", []))
    if stale_cost > totals["total_cost"] * 0.05:
        savings = stale_cost * 0.4  # rough — clearing context would cut bloat substantially
        candidates.append({
            "kind": "stale_context",
            "savings": savings,
            "headline": f"Context bloat is costing ~${savings:.0f} this period",
            "action": "Use /clear when switching tasks or start fresh sessions for unrelated work.",
            "evidence": f"{len([s for s in sessions if 'stale-context' in s.get('flags',[])])} session(s) averaged >300K cache reads per turn.",
        })

    # Opus on trivial work.
    opus_trivial_cost = 0
    for s in sessions:
        if s.get("opus_total_tokens", 0) > 0:
            ratio = s["opus_trivial_tokens"] / s["opus_total_tokens"]
            opus_trivial_cost += sum(c for m, c in s["by_model_cost"].items() if "opus" in m.lower()) * ratio
    if opus_trivial_cost > totals["total_cost"] * 0.05:
        savings = opus_trivial_cost * 0.6  # Sonnet is ~40% cost
        candidates.append({
            "kind": "opus_trivial",
            "savings": savings,
            "headline": f"Opus on trivial reads/searches costs ~${savings:.0f}",
            "action": "For simple file lookups, Bash, or Grep-only turns, switch to Sonnet (~40% cheaper).",
            "evidence": f"~${opus_trivial_cost:.0f} of Opus tokens went to read-only or Bash-only turns.",
        })

    # External services (MCP).
    mcp_cost_est = totals["mcp_tokens"] * 6.25 / 1e6  # conservative cache-write rate
    if mcp_cost_est > totals["total_cost"] * 0.10:
        top_mcp = totals["mcp_heavy_tools"][:2]
        savings = mcp_cost_est * 0.4  # request summaries instead of full content
        names = ", ".join(FRIENDLY_MCP.get(n, n) for n, _ in top_mcp)
        candidates.append({
            "kind": "mcp_heavy",
            "savings": savings,
            "headline": f"External services pulled ~${mcp_cost_est:.0f} of tokens",
            "action": f"Ask for summaries or specific ranges instead of full documents — especially {names}.",
            "evidence": f"~{totals['mcp_tokens']//1000}K tokens of tool results across {totals['total_mcp']} calls.",
        })

    # Many short sessions.
    avg_msgs = totals["total_messages"] / max(totals["sessions"], 1)
    if avg_msgs < 5 and totals["sessions"] >= 5:
        savings = totals["total_cost"] * 0.15  # rough — context rebuilding cost
        candidates.append({
            "kind": "short_sessions",
            "savings": savings,
            "headline": f"Many short sessions burn ~${savings:.0f} on rebuilt context",
            "action": "Batch related tasks into one longer session — context gets re-loaded each time you start fresh.",
            "evidence": f"Average {avg_msgs:.1f} messages per session across {totals['sessions']} sessions.",
        })

    # Vague prompts.
    if totals["total_prompts"] > 0 and totals["short_prompts"] / totals["total_prompts"] > 0.20:
        savings = totals["total_cost"] * 0.08
        pct = totals["short_prompts"] / totals["total_prompts"]
        candidates.append({
            "kind": "vague_prompts",
            "savings": savings,
            "headline": f"Vague prompts may cost ~${savings:.0f} in re-tries",
            "action": "Be specific upfront — file paths, expected output, constraints.",
            "evidence": f"{pct:.0%} of your prompts were under 20 characters.",
        })

    if not candidates:
        return None
    candidates.sort(key=lambda x: -x["savings"])
    return candidates[0]


# ── Best moments ─────────────────────────────────────────────────────────────

def compute_best_moments(sessions):
    """Return up to 2 sessions that exemplify good usage."""
    out = []
    eligible = [s for s in sessions if s["messages_assistant"] >= 5 and not s.get("flags")]
    if not eligible:
        return out

    # Longest deep-work session.
    by_dur = sorted(eligible, key=lambda s: -s["duration_min"])
    if by_dur and by_dur[0]["duration_min"] >= 30:
        s = by_dur[0]
        out.append({
            "kind": "deep_work",
            "title": "Longest clean session",
            "label": s["label"], "session_id": s["session_id"],
            "duration_min": s["duration_min"], "messages": s["messages_user"] + s["messages_assistant"],
            "cost": s["cost_total"], "writes": s["writes"], "category": s["category"],
        })

    # Highest output-per-dollar (productivity proxy).
    def yield_score(s):
        return (s["writes"] * 5 + s["messages_assistant"]) / max(s["cost_total"], 0.01)
    by_yield = sorted([s for s in eligible if s["cost_total"] > 0.50], key=lambda s: -yield_score(s))
    if by_yield and (not out or by_yield[0]["session_id"] != out[0]["session_id"]):
        s = by_yield[0]
        out.append({
            "kind": "high_yield",
            "title": "Most productive session",
            "label": s["label"], "session_id": s["session_id"],
            "duration_min": s["duration_min"], "messages": s["messages_user"] + s["messages_assistant"],
            "cost": s["cost_total"], "writes": s["writes"], "category": s["category"],
        })

    return out


# ── Targets ──────────────────────────────────────────────────────────────────

def compute_hero_headline(totals, cost_breakdown, hours, sessions, num_days):
    """Pick one insight-shaped headline from a few candidates.

    Each candidate is scored by how *striking* it is — bigger ratios, larger
    numbers, sharper concentration. The most striking wins. Fallbacks to a
    descriptive line if nothing is interesting.
    """
    cw = (cost_breakdown.get("cache_write_5m", 0) or 0) + (cost_breakdown.get("cache_write_1h", 0) or 0)
    cr = cost_breakdown.get("cache_read", 0) or 0
    out = cost_breakdown.get("output", 0) or 0
    cache_total = cr + cw
    total = totals["total_cost"]

    candidates = []

    # 1. Cache vs output framing.
    if out > 0 and cache_total > out * 3:
        ratio = cache_total / out
        candidates.append({
            "score": min(ratio / 3, 5),
            "text": f"Cache reads and writes cost {ratio:.1f}× what Claude's actual responses cost (${cache_total:.0f} vs ${out:.0f}).",
        })

    # 2. Cache % of total spend.
    if total > 0 and cache_total / total > 0.80:
        pct = cache_total / total
        candidates.append({
            "score": pct * 4,
            "text": f"{pct:.0%} of your spend goes to re-reading conversation history each turn — output and prompts are the cheap part.",
        })

    # 3. Concentration: top 5 sessions vs the rest.
    sorted_costs = sorted([s["cost_total"] for s in sessions], reverse=True)
    if len(sorted_costs) >= 10:
        top5 = sum(sorted_costs[:5])
        rest = sum(sorted_costs[5:])
        if top5 > rest:
            candidates.append({
                "score": (top5 / max(rest, 0.01)) * 1.5,
                "text": f"Your top 5 sessions cost more than the other {len(sorted_costs) - 5} combined (${top5:.0f} vs ${rest:.0f}).",
            })

    # 4. Hours framing — claude-as-percent-of-work-week.
    if hours > 5 and num_days >= 7:
        weeks = num_days / 7
        hours_per_week = hours / weeks
        if hours_per_week > 2:
            pct_of_workweek = hours_per_week / 40
            candidates.append({
                "score": min(hours_per_week / 5, 4),
                "text": f"You spent {hours:.0f} hours in Claude over {num_days} days — about {pct_of_workweek:.0%} of a 40-hour work week.",
            })

    # 5. Cost per active day.
    if total > 0 and totals["active_days"] > 0:
        cpd = total / totals["active_days"]
        if cpd > 20:
            candidates.append({
                "score": min(cpd / 30, 3),
                "text": f"You spent about ${cpd:.0f}/active day on Claude over this period.",
            })

    # 6. MCP/external services dominating.
    mcp_cost_est = totals.get("mcp_tokens", 0) * 6.25 / 1e6
    if total > 0 and mcp_cost_est > total * 0.15:
        candidates.append({
            "score": (mcp_cost_est / total) * 4,
            "text": f"External services (Slack, Snowflake, Google Docs, etc.) account for ~${mcp_cost_est:.0f} of token spend — about {mcp_cost_est/total:.0%} of total.",
        })

    if not candidates:
        return f"${total:.2f} of token spend across {len(sessions)} sessions and {hours:.1f} hours of active use."

    candidates.sort(key=lambda x: -x["score"])
    return candidates[0]["text"]


def compute_targets(totals, sessions, num_days):
    """Return progress vs target for a few key metrics.

    Targets are usage-pattern guides, not productivity quotas. Consistency-style
    metrics ("days/week") are intentionally omitted — a weekend hobbyist and a
    daily user shouldn't both be told they're falling short of a 5-day office
    benchmark.
    """
    cache_rate = totals["cache_hit_rate"]
    opus_trivial_pct = totals["opus_trivial_pct"]
    avg_session_depth = totals["total_messages"] / max(totals["sessions"], 1)

    return [
        {"name": "Cache hit rate", "value": cache_rate, "target": 0.80, "fmt": "pct",
         "tip": "Higher = more context reused across messages. Long focused sessions help."},
        {"name": "Avg session depth", "value": avg_session_depth, "target": 15, "fmt": "msgs",
         "tip": "Longer sessions amortize context-loading cost. Short sessions reload everything."},
        {"name": "Opus on trivial work", "value": opus_trivial_pct, "target": 0.15, "fmt": "pct_inv",
         "tip": "Lower = better. % of Opus tokens spent on read-only or Bash-only turns where Sonnet would suffice."},
    ]


# ── Analyze ──────────────────────────────────────────────────────────────────

def analyze(all_sessions, date_from, date_to):
    sliced = slice_sessions(all_sessions, date_from, date_to)
    if not sliced:
        return {"error": "No sessions found in this date range.", "sessions": 0}

    period_days = (date_to - date_from).days
    prev_from = date_from - timedelta(days=period_days + 1)
    prev_to = date_from - timedelta(days=1)
    prev_sliced = slice_sessions(all_sessions, prev_from, prev_to)

    sessions = [s for s, _ in sliced]

    ti = sum(s["tokens"]["input"] for s in sessions)
    to_ = sum(s["tokens"]["output"] for s in sessions)
    tcr = sum(s["tokens"]["cache_read"] for s in sessions)
    tcw5 = sum(s["tokens"]["cache_write_5m"] for s in sessions)
    tcw1 = sum(s["tokens"]["cache_write_1h"] for s in sessions)
    tcw = tcw5 + tcw1
    ttok = ti + to_ + tcr + tcw
    tcost = sum(s["cost_total"] for s in sessions)
    t_user = sum(s["messages_user"] for s in sessions)
    t_asst = sum(s["messages_assistant"] for s in sessions)
    t_dur = sum(s["duration_min"] for s in sessions)
    t_tools = sum(sum(s["tool_calls"].values()) for s in sessions)

    cost_input = sum(s["cost"]["input"] for s in sessions)
    cost_output = sum(s["cost"]["output"] for s in sessions)
    cost_cache_read = sum(s["cost"]["cache_read"] for s in sessions)
    cost_cache_write_5m = sum(s["cost"]["cache_write_5m"] for s in sessions)
    cost_cache_write_1h = sum(s["cost"]["cache_write_1h"] for s in sessions)
    cost_web_search = sum(s["cost"]["web_search"] for s in sessions)
    web_search_n = sum(s["tokens"].get("web_search_requests", 0) for s in sessions)

    all_tools = defaultdict(int)
    for s in sessions:
        for t, c in s["tool_calls"].items():
            all_tools[t] += c

    model_tokens = defaultdict(int)
    model_cost = defaultdict(float)
    for s in sessions:
        for m, t in s["by_model_tokens"].items():
            model_tokens[m] += t
        for m, c in s["by_model_cost"].items():
            model_cost[m] += c

    cache_denom = tcr + tcw + ti
    cache_rate = tcr / cache_denom if cache_denom > 0 else 0
    tpm = to_ / t_asst if t_asst > 0 else 0
    cps = tcost / len(sessions)
    mps = (t_user + t_asst) / len(sessions)
    num_days = (date_to - date_from).days + 1
    proj_monthly = (tcost / num_days) * 30 if num_days > 0 else tcost
    proj_yearly = proj_monthly * 12

    opus_tok = sum(t for m, t in model_tokens.items() if "opus" in m.lower())
    opus_pct = opus_tok / ttok if ttok > 0 else 0
    opus_cost = sum(c for m, c in model_cost.items() if "opus" in m.lower())
    opus_total_w = sum(s.get("opus_total_tokens", 0) for s in sessions)
    opus_trivial_w = sum(s.get("opus_trivial_tokens", 0) for s in sessions)
    opus_trivial_pct = opus_trivial_w / opus_total_w if opus_total_w > 0 else 0

    daily = defaultdict(lambda: {"sessions": set(), "tokens": 0, "cost": 0.0, "msgs": 0})
    for s, in_dates in sliced:
        sid = s["session_id"] or id(s)
        for d, dd in in_dates.items():
            daily[d]["sessions"].add(sid)
            daily[d]["tokens"] += dd["tokens"]
            daily[d]["cost"] += dd["cost"]
            daily[d]["msgs"] += dd["msgs"]
    daily_list = [{"date": k, "sessions": len(v["sessions"]), "tokens": v["tokens"],
                   "cost": v["cost"], "msgs": v["msgs"]} for k, v in sorted(daily.items())]

    model_list = [{"model": m, "friendly": friendly_model_name(m), "tokens": t,
                   "cost": model_cost.get(m, 0), "pct": t / ttok if ttok > 0 else 0}
                  for m, t in sorted(model_tokens.items(), key=lambda x: -x[1])]

    sorted_tools = sorted(all_tools.items(), key=lambda x: -x[1])[:12]
    tools_list = [{"name": t.replace("mcp__", "").replace("__", "::"),
                   "friendly": friendly_tool_name(t),
                   "raw": t, "count": c} for t, c in sorted_tools]

    u_score = score_usage(sliced, num_days)
    e_score = score_efficiency(sliced)
    c_score = composite_score(u_score, e_score)

    total_mcp = sum(s["mcp_calls"] for s in sessions)
    total_mcp_tokens = sum(s["mcp_tokens"] for s in sessions)

    mcp_tool_tokens = defaultdict(int)
    for s in sessions:
        for t, tok in s["mcp_tool_tokens"].items():
            mcp_tool_tokens[t] += tok
    mcp_heavy_tools = sorted(
        [(t.replace("mcp__", "").replace("__", "::"), tok) for t, tok in mcp_tool_tokens.items()],
        key=lambda x: -x[1])[:5]
    mcp_heavy_friendly = [
        {"name": n, "friendly": FRIENDLY_MCP.get(n, n), "tokens": tok,
         "est_cost": tok * 6.25 / 1e6} for n, tok in mcp_heavy_tools
    ]

    active_days = len(daily_list)

    totals = {
        "total_cost": tcost, "total_messages": t_user + t_asst, "sessions": len(sessions),
        "total_prompts": sum(s["total_prompts"] for s in sessions),
        "short_prompts": sum(s["short_prompts"] for s in sessions),
        "mcp_tokens": total_mcp_tokens, "total_mcp": total_mcp,
        "mcp_heavy_tools": mcp_heavy_tools,
        "cache_hit_rate": cache_rate, "active_days": active_days,
        "opus_trivial_pct": opus_trivial_pct,
    }

    total_hours = t_dur / 60
    top_finding = compute_top_finding(sessions, totals)
    best_moments = compute_best_moments(sessions)
    targets = compute_targets(totals, sessions, num_days)
    hero_headline = compute_hero_headline(
        totals,
        {"cache_read": cost_cache_read, "cache_write_5m": cost_cache_write_5m,
         "cache_write_1h": cost_cache_write_1h, "output": cost_output},
        total_hours, sessions, num_days,
    )

    # Category aggregation
    by_category = defaultdict(lambda: {"sessions": 0, "cost": 0.0, "messages": 0})
    for s in sessions:
        c = s["category"]
        by_category[c]["sessions"] += 1
        by_category[c]["cost"] += s["cost_total"]
        by_category[c]["messages"] += s["messages_user"] + s["messages_assistant"]
    categories = [{"name": k, **v, "cost_pct": v["cost"] / tcost if tcost > 0 else 0}
                  for k, v in sorted(by_category.items(), key=lambda x: -x[1]["cost"])]

    # Recommendations — deduped, with $ savings where possible.
    recs = {"primary": [], "secondary": [], "good": []}

    if cache_rate >= 0.85:
        recs["good"].append({"title": "Excellent cache reuse",
            "body": f"Cache hit rate of {cache_rate:.0%} — context is being reused well across messages."})
    elif cache_rate < 0.65:
        savings = tcost * 0.10
        recs["primary"].append({"title": "Low cache hit rate",
            "body": f"Cache hit rate is {cache_rate:.0%}. Longer sessions amortize the context-loading cost. Estimated impact: ~${savings:.0f}.",
            "savings": savings})

    if mps >= 50:
        recs["good"].append({"title": "Deep, focused sessions",
            "body": f"Averaging {mps:.0f} messages per session — sustained work that amortizes context costs."})
    elif mps < 5:
        savings = tcost * 0.15
        recs["primary"].append({"title": "Many short sessions",
            "body": f"Only {mps:.1f} messages per session. Batch related tasks together. Estimated impact: ~${savings:.0f}.",
            "savings": savings})

    if 0 < tpm <= 300:
        recs["good"].append({"title": "Lean responses",
            "body": f"Output averages {tpm:.0f} tokens/message — concise and targeted."})
    elif tpm > 5000:
        savings = (tpm - 1500) * t_asst / 1e6 * 25 * 0.5
        recs["secondary"].append({"title": "Verbose responses",
            "body": f"Output averages {tpm:,.0f} tokens/message. Asking for shorter answers could save ~${savings:.0f}.",
            "savings": savings})

    if cps > 5.0:
        recs["secondary"].append({"title": "High session cost",
            "body": f"Average ${cps:.2f}/session. Consider focused, task-specific sessions over long exploratory ones."})

    stale_n = len([s for s in sessions if "stale-context" in s.get("flags", [])])
    if stale_n:
        savings = sum(s["cost_total"] for s in sessions if "stale-context" in s.get("flags", [])) * 0.4
        recs["primary"].append({"title": f"Stale context in {stale_n} session(s)",
            "body": f"These sessions averaged >300K cache reads per turn — Claude is re-reading content it no longer needs. Use /clear when switching tasks. Estimated impact: ~${savings:.0f}.",
            "savings": savings})

    if opus_total_w > 0 and opus_trivial_pct >= 0.30:
        savings = opus_cost * opus_trivial_pct * 0.6
        recs["primary"].append({"title": "Opus on trivial work",
            "body": f"{opus_trivial_pct:.0%} of Opus tokens went to read-only/Bash-only turns. Sonnet would suffice. Estimated impact: ~${savings:.0f}.",
            "savings": savings})
    elif opus_total_w > 0 and opus_trivial_pct <= 0.15:
        recs["good"].append({"title": "Opus used where it counts",
            "body": f"Only {opus_trivial_pct:.0%} of Opus tokens went to trivial turns — most Opus was on substantive work."})

    if total_mcp > 0 and (total_mcp_tokens > 100_000 or total_mcp > 100):
        mcp_cost = total_mcp_tokens * 6.25 / 1e6
        savings = mcp_cost * 0.4
        top = ", ".join(t["friendly"] for t in mcp_heavy_friendly[:2])
        recs["primary"].append({"title": "External services pulled large content",
            "body": f"~{total_mcp_tokens // 1000}K tokens of tool results across {total_mcp} calls (~${mcp_cost:.0f}). Ask for summaries or specific ranges. Heaviest: {top}. Estimated impact: ~${savings:.0f}.",
            "savings": savings})

    flagged = [s for s in sessions if s.get("flags")]
    flag_counts = defaultdict(int)
    for s in flagged:
        for f in s["flags"]:
            flag_counts[f] += 1

    if flag_counts.get("bulk-read-no-output", 0) > 0:
        recs["secondary"].append({"title": "Bulk reads without output",
            "body": f"{flag_counts['bulk-read-no-output']} session(s) had 5+ file reads but no edits. Fine for research; otherwise it's exploration without a deliverable."})
    if flag_counts.get("redundant-reads", 0) > 0:
        n = sum(s["redundant_reads"] for s in flagged)
        recs["secondary"].append({"title": "Redundant file reads",
            "body": f"{n} times a file was re-read identically in the same session. Reference content already in context instead."})
    if flag_counts.get("low-interaction", 0) > 0:
        recs["secondary"].append({"title": "Low-interaction sessions",
            "body": f"{flag_counts['low-interaction']} session(s) had fewer than 3 user messages but 10+ from Claude. Check that you're guiding the work."})
    if flag_counts.get("truncated-output", 0) > 0:
        recs["secondary"].append({"title": "Truncated responses",
            "body": f"{flag_counts['truncated-output']} session(s) had assistant turns cut off at max_tokens. Ask for shorter outputs or break the request up."})
    total_interrupts = sum(s["interrupts"] for s in sessions)
    if total_interrupts > len(sessions) * 0.2:
        recs["secondary"].append({"title": "Frequent interrupts",
            "body": f"{total_interrupts} cancelled requests. Each interrupt wastes the tokens already spent. Be specific upfront."})
    if sum(s["plan_mode_uses"] for s in sessions) > 0:
        recs["good"].append({"title": "Using plan mode",
            "body": f"{sum(s['plan_mode_uses'] for s in sessions)} plan-mode invocations. One of the cheapest ways to align before expensive work."})

    if proj_monthly > 200:
        recs["secondary"].append({"title": "High monthly projection",
            "body": f"At this pace, ~${proj_monthly:.0f}/mo (~${proj_yearly:.0f}/yr). Not a problem if output justifies it."})

    short_pct = totals["short_prompts"] / max(totals["total_prompts"], 1)
    detailed = sum(s["detailed_prompts"] for s in sessions) / max(totals["total_prompts"], 1)
    if short_pct > 0.20:
        savings = tcost * 0.08
        recs["secondary"].append({"title": "Vague prompts",
            "body": f"{round(short_pct * 100)}% of your prompts are under 20 characters. Specific prompts get the right answer first try. Estimated impact: ~${savings:.0f}.",
            "savings": savings})
    elif detailed > 0.20:
        recs["good"].append({"title": "Specific prompts",
            "body": f"{round(detailed * 100)}% of your prompts are 500+ characters."})

    # Trend
    trend = None
    if prev_sliced:
        prev_sessions = [s for s, _ in prev_sliced]
        p_out = sum(s["tokens"]["output"] for s in prev_sessions)
        p_asst = sum(s["messages_assistant"] for s in prev_sessions)
        p_tpm = p_out / p_asst if p_asst > 0 else 0
        p_cr = sum(s["tokens"]["cache_read"] for s in prev_sessions)
        p_cw = sum(s["tokens"]["cache_write_5m"] + s["tokens"]["cache_write_1h"] for s in prev_sessions)
        p_inp = sum(s["tokens"]["input"] for s in prev_sessions)
        p_denom = p_cr + p_cw + p_inp
        p_cache = p_cr / p_denom if p_denom > 0 else 0
        p_cost = sum(s["cost_total"] for s in prev_sessions)
        p_cps = p_cost / len(prev_sessions)
        trend = {
            "prev_tokens_per_msg": p_tpm, "curr_tokens_per_msg": tpm,
            "prev_cache_rate": p_cache, "curr_cache_rate": cache_rate,
            "prev_cost_per_session": p_cps, "curr_cost_per_session": cps,
            "prev_total_cost": p_cost, "curr_total_cost": tcost,
            "prev_sessions": len(prev_sessions), "curr_sessions": len(sessions),
        }

    # Top expensive turns + drill-down link
    all_turns = []
    for s in sessions:
        for t in s["per_turn_cost"]:
            all_turns.append({**t, "session_label": s["label"], "session_category": s["category"]})
    top_turns = sorted(all_turns, key=lambda x: -x["cost"])[:10]

    # Bloat curves
    bloat_sessions = sorted(
        [s for s in sessions if s["avg_cache_read_per_turn"] > 50_000],
        key=lambda s: -s["avg_cache_read_per_turn"]
    )[:3]
    bloat_curves = []
    for s in bloat_sessions:
        pts = s["per_turn_cache_read"]
        if len(pts) > 30:
            step = max(1, len(pts) // 30)
            pts = pts[::step]
        bloat_curves.append({
            "session_id": s["session_id"], "label": s["label"], "category": s["category"],
            "project": project_name_from_dir(s["project_dir"]) if s["project_dir"] else "?",
            "messages": s["messages_user"] + s["messages_assistant"],
            "cost": s["cost_total"],
            "avg_cache_read": s["avg_cache_read_per_turn"],
            "max_cache_read": s["max_cache_read_per_turn"],
            "points": [{"idx": i, "cr": cr} for i, cr in pts],
        })

    # Project breakdown
    proj_groups = defaultdict(list)
    for s in sessions:
        proj_groups[s["project_dir"]].append(s)
    projects = []
    for pdir, psessions in proj_groups.items():
        sliced_p = [(s, dict(s["by_date"])) for s in psessions]
        p_cost = sum(s["cost_total"] for s in psessions)
        p_tok = sum(sum(s["tokens"][k] for k in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")) for s in psessions)
        p_msgs = sum(s["messages_user"] + s["messages_assistant"] for s in psessions)
        p_writes = sum(s["writes"] for s in psessions)
        p_reads = sum(s["reads"] for s in psessions)
        p_mcp = sum(s["mcp_calls"] for s in psessions)
        p_dur = sum(s["duration_min"] for s in psessions)
        p_eff = score_efficiency(sliced_p)
        projects.append({
            "name": project_name_from_dir(pdir),
            "sessions": len(psessions), "cost": p_cost, "tokens": p_tok,
            "messages": p_msgs, "writes": p_writes, "reads": p_reads,
            "mcp_calls": p_mcp, "duration_min": p_dur, "efficiency": p_eff,
            "cost_pct": p_cost / tcost if tcost > 0 else 0,
        })
    projects.sort(key=lambda x: -x["cost"])

    # Profile labels (descriptive, not graded)
    usage_band = _band(u_score, USAGE_BANDS)
    eff_band = _band(e_score, EFFICIENCY_BANDS)
    composite_band = _band(c_score, COMPOSITE_BANDS)

    # Empty-state thresholds
    is_low_data = len(sessions) < 5

    return {
        "period": {"from": str(date_from), "to": str(date_to), "days": num_days, "active_days": active_days},
        "summary": {
            "sessions": len(sessions), "total_tokens": ttok,
            "total_cost": tcost, "total_user_msgs": t_user,
            "total_asst_msgs": t_asst, "total_tool_calls": t_tools,
            "total_duration_min": t_dur,
            "total_hours": round(total_hours, 1),
            "tokens_input": ti, "tokens_output": to_,
            "tokens_cache_read": tcr,
            "tokens_cache_write_5m": tcw5, "tokens_cache_write_1h": tcw1,
            "web_search_requests": web_search_n,
        },
        "efficiency": {
            "tokens_per_msg": tpm, "cost_per_session": cps,
            "cache_hit_rate": cache_rate, "msgs_per_session": mps,
            "avg_duration_min": t_dur / len(sessions),
            "projected_monthly": proj_monthly,
            "projected_yearly": proj_yearly,
            "opus_trivial_pct": opus_trivial_pct,
        },
        "scores": {
            "usage": u_score, "efficiency": e_score, "composite": c_score,
            "usage_band": usage_band, "efficiency_band": eff_band, "composite_band": composite_band,
        },
        "cost_breakdown": {
            "input": cost_input, "output": cost_output,
            "cache_read": cost_cache_read,
            "cache_write_5m": cost_cache_write_5m, "cache_write_1h": cost_cache_write_1h,
            "web_search": cost_web_search,
        },
        "hero_headline": hero_headline,
        "top_finding": top_finding,
        "best_moments": best_moments,
        "targets": targets,
        "categories": categories,
        "models": model_list,
        "opus_pct": opus_pct,
        "tools": tools_list,
        "daily": daily_list,
        "trend": trend,
        "recs": recs,
        "projects": projects,
        "session_health": {
            "total_reads": sum(s["reads"] for s in sessions),
            "total_writes": sum(s["writes"] for s in sessions),
            "total_mcp": total_mcp,
            "total_mcp_tokens": total_mcp_tokens,
            "mcp_heavy_tools": mcp_heavy_friendly,
            "total_interrupts": total_interrupts,
            "flagged_count": len(flagged),
            "clean_pct": round((1 - len(flagged) / len(sessions)) * 100),
            "redundant_reads": sum(s["redundant_reads"] for s in sessions),
            "subagent_spawns": sum(s["subagent_spawns"] for s in sessions),
            "skill_invocations": sum(s["skill_invocations"] for s in sessions),
            "top_skills": sorted(
                [(k, sum(s["skills_used"].get(k, 0) for s in sessions))
                 for k in {k for s in sessions for k in s["skills_used"].keys()}],
                key=lambda x: -x[1])[:5],
            "top_flags": sorted(
                [(flag, sum(1 for s in sessions if flag in s.get("flags", [])))
                 for flag in {f for s in sessions for f in s.get("flags", [])}],
                key=lambda x: -x[1])[:3],
            "plan_mode_uses": sum(s["plan_mode_uses"] for s in sessions),
            "truncated_turns": sum(s["truncated_turns"] for s in sessions),
            "stale_context_sessions": stale_n,
        },
        "top_turns": [{
            "cost": t["cost"], "model": t["model"],
            "model_friendly": friendly_model_name(t["model"]),
            "tool": t["tool"], "tool_friendly": friendly_tool_name(t["tool"]),
            "session_id": t["session_id"], "session_label": t["session_label"],
            "session_category": t["session_category"],
            "ts": t["ts"], "cache_read": t["cache_read"], "output": t["output"],
            "cache_write": t["cache_write"], "driver": t["driver"],
        } for t in top_turns],
        "bloat_curves": bloat_curves,
        "is_low_data": is_low_data,
    }


def session_detail(all_sessions, session_id):
    """Return per-turn breakdown for a single session."""
    for s in all_sessions:
        if s.get("session_id") == session_id:
            return {
                "session_id": s["session_id"],
                "label": s["label"], "category": s["category"],
                "project": project_name_from_dir(s["project_dir"]) if s["project_dir"] else "?",
                "first_prompt": s["first_prompt"],
                "messages_user": s["messages_user"],
                "messages_assistant": s["messages_assistant"],
                "duration_min": s["duration_min"],
                "cost": s["cost_total"],
                "cost_breakdown": dict(s["cost"]),
                "tokens": dict(s["tokens"]),
                "tools": [{"name": t, "friendly": friendly_tool_name(t), "count": c}
                          for t, c in sorted(s["tool_calls"].items(), key=lambda x: -x[1])[:15]],
                "flags": s["flags"],
                "turns": [{
                    "idx": t["idx"], "cost": t["cost"],
                    "model_friendly": friendly_model_name(t["model"]),
                    "tool_friendly": friendly_tool_name(t["tool"]),
                    "cache_read": t["cache_read"],
                    "cache_write": t["cache_write"],
                    "output": t["output"], "driver": t["driver"],
                    "ts": t["ts"],
                } for t in s["per_turn_cost"]],
            }
    return {"error": "Session not found"}


# ── Demo data fixture ───────────────────────────────────────────────────────
# Synthetic sessions for screenshots, demos, and previewing the tool before
# you have logs of your own. Deterministic (seeded), no real data. Three
# personas so the preview matches the user's reality:
#   - solo-dev (default): hobbyist or solo developer; mostly code, no
#     enterprise MCP servers. Modal Claude Code user.
#   - pm: product manager at a tech company; meetings, data pulls, design
#     reviews, mixed MCP stack.
#   - writer: content creator / researcher; lots of writing and research,
#     light coding.

DEMO_PROJECT_DIRS_SOLO = {
    "/demo/projects/my-app":           "my-app",
    "/demo/projects/portfolio-site":   "portfolio-site",
    "/demo/projects/cli-tool":         "cli-tool",
    "/demo/projects/learning":         "learning",
}
DEMO_PROJECT_DIRS_PM = {
    "/demo/projects/launch-prep":      "launch-prep",
    "/demo/projects/data-pipeline":    "data-pipeline",
    "/demo/projects/roadmap":          "roadmap",
    "/demo/projects/design-review":    "design-review",
}
DEMO_PROJECT_DIRS_WRITER = {
    "/demo/projects/newsletter":       "newsletter",
    "/demo/projects/research-notes":   "research-notes",
    "/demo/projects/personal-site":    "personal-site",
}

DEMO_PERSONAS = {
    "solo-dev": {
        "project_dirs": DEMO_PROJECT_DIRS_SOLO,
        "categories": ["Coding", "Debugging", "Learning", "Research", "Personal Project", "Writing"],
        "weights": [50, 18, 14, 8, 6, 4],
        "labels": {
            "Coding": [
                "Refactor the auth module to support OAuth providers",
                "Add unit tests for the user service edge cases",
                "Fix the race condition in the worker queue",
                "Wire up the Stripe webhook handler with retry logic",
                "Migrate the logger to structured JSON logs",
                "Rewrite the rate-limiter as a sliding window",
                "Add pagination to the products endpoint",
                "Set up GitHub Actions CI for the test suite",
            ],
            "Debugging": [
                "Debug why my Postgres query is hanging in production",
                "Investigate the memory leak in the worker process",
                "Track down why the deploy keeps failing on the build step",
                "Why does my React component re-render 12 times on mount?",
            ],
            "Learning": [
                "Walk me through how React Server Components actually work",
                "Teach me what fiber is in the JS event loop",
                "What's the difference between SSE and WebSocket?",
                "How do I structure a Next.js app with Server Actions?",
                "Explain how Postgres MVCC works with examples",
            ],
            "Research": [
                "Research the best embedding models for code search in 2026",
                "Compare Vercel vs Cloudflare vs Fly.io for a Node app",
                "Look into how Linear handles real-time sync under the hood",
            ],
            "Personal Project": [
                "Build the landing page for my side project",
                "Add a Stripe checkout flow to my hobby site",
                "Set up the database schema for my note-taking app",
                "Wire up auth on my weekend project",
            ],
            "Writing": [
                "Draft the README for my open source library",
                "Write the launch post for my side project",
            ],
        },
        "tool_profiles": {
            "Coding":           {"Edit": (5, 25), "Write": (1, 6), "Read": (8, 30), "Bash": (3, 12), "Grep": (1, 8), "Glob": (1, 4)},
            "Debugging":        {"Read": (10, 30), "Bash": (5, 18), "Grep": (3, 10), "Edit": (1, 6)},
            "Learning":         {"Read": (2, 8), "WebSearch": (1, 4), "WebFetch": (0, 3)},
            "Research":         {"WebSearch": (2, 6), "WebFetch": (1, 4), "Read": (1, 5)},
            "Personal Project": {"Edit": (3, 14), "Write": (1, 4), "Read": (4, 15), "Bash": (2, 8), "Grep": (1, 5)},
            "Writing":          {"Read": (3, 10), "Edit": (1, 4), "Write": (0, 2)},
        },
    },
    "pm": {
        "project_dirs": DEMO_PROJECT_DIRS_PM,
        "categories": ["Coding", "Data Analysis", "PM Work", "Design Work", "Debugging", "Research", "Writing"],
        "weights": [25, 22, 22, 8, 8, 8, 7],
        "labels": {
            "Coding": [
                "Prototype the new admin CLI for tenant onboarding",
                "Build a quick script to dedupe the customer export",
            ],
            "Data Analysis": [
                "Investigate why DAU dropped on Tuesday — is it real?",
                "Pull conversion metrics for the Q2 leadership review",
                "Build the cohort retention query for engagement analysis",
                "Compare regional performance week over week",
                "Size the A/B test for the new checkout flow",
            ],
            "PM Work": [
                "Prep for the leadership weekly — agenda and updates",
                "Digest the planning meeting and update the brief",
                "Draft the launch email for the new feature",
                "Investigate the customer escalation about exports",
                "Run the morning sync — review yesterday and surface action items",
            ],
            "Design Work": [
                "Review the new onboarding flow in Figma",
                "Iterate on the dashboard layout for narrow viewports",
            ],
            "Debugging": [
                "Debug why the staging deploy keeps failing this morning",
                "Investigate the memory leak in the worker process",
            ],
            "Research": [
                "Research how competitors handle multi-tenant data isolation",
                "Look into emerging gRPC streaming patterns",
            ],
            "Writing": [
                "Draft the design doc for cache invalidation across services",
                "Write the post-mortem for last week's incident",
            ],
        },
        "tool_profiles": {
            "Coding":        {"Edit": (5, 25), "Write": (1, 6), "Read": (8, 30), "Bash": (3, 12), "Grep": (1, 8), "Glob": (1, 4)},
            "Data Analysis": {"mcp__snowflake__run_snowflake_query": (3, 12), "mcp__snowflake__list_objects": (1, 4), "Read": (2, 8), "Edit": (0, 3)},
            "PM Work":       {"mcp__granola__get_meeting_transcript": (1, 4), "mcp__slack__slack_read_thread": (2, 7), "mcp__google-workspace__editGoogleDoc": (1, 4), "mcp__google-workspace__readGoogleDoc": (1, 5), "Read": (1, 5)},
            "Design Work":   {"mcp__figma__get_design_context": (1, 4), "mcp__figma__get_screenshot": (0, 3), "Read": (1, 5)},
            "Debugging":     {"Read": (10, 30), "Bash": (5, 18), "Grep": (3, 10), "Edit": (1, 6)},
            "Research":      {"mcp__glean_default__search": (2, 8), "mcp__glean_default__read_document": (1, 5), "WebSearch": (1, 4), "Read": (3, 10)},
            "Writing":       {"mcp__google-workspace__editGoogleDoc": (1, 4), "Read": (3, 12), "Edit": (1, 4), "Write": (0, 2)},
        },
    },
    "writer": {
        "project_dirs": DEMO_PROJECT_DIRS_WRITER,
        "categories": ["Writing", "Research", "Learning", "Coding", "Personal Project"],
        "weights": [42, 28, 14, 10, 6],
        "labels": {
            "Writing": [
                "Draft this week's newsletter on AI infra economics",
                "Rewrite the intro to my long-form essay on agentic search",
                "Polish the outline for next month's deep-dive piece",
                "Draft a Twitter thread summarizing the new paper",
                "Write the LinkedIn post about my latest experiment",
            ],
            "Research": [
                "Compare claims across the three major LLM scaling papers",
                "Research how Cloudflare Workers handle WebSocket fanout",
                "Pull together a primer on prompt-caching pricing models",
            ],
            "Learning": [
                "Teach me how attention sinks actually work in long context",
                "What's the trade-off between RAG and long context for my use case?",
                "Walk me through MCP server architecture",
            ],
            "Coding": [
                "Set up a small static site for my newsletter archive",
                "Write a script to scrape my Substack analytics",
            ],
            "Personal Project": [
                "Build the visualization for my next post",
                "Wire up an RSS feed for my personal site",
            ],
        },
        "tool_profiles": {
            "Writing":          {"Edit": (3, 12), "Write": (1, 5), "Read": (3, 10), "WebFetch": (0, 3)},
            "Research":         {"WebSearch": (3, 10), "WebFetch": (2, 8), "Read": (2, 6)},
            "Learning":         {"WebSearch": (1, 4), "Read": (2, 6)},
            "Coding":           {"Edit": (3, 14), "Write": (1, 4), "Read": (4, 15), "Bash": (2, 8)},
            "Personal Project": {"Edit": (2, 10), "Write": (1, 4), "Read": (3, 12), "Bash": (1, 5)},
        },
    },
}


def generate_demo_sessions(n_sessions=85, end_date=None, seed=42, persona="solo-dev"):
    """Build deterministic synthetic session records that match the parser shape."""
    rng = random.Random(seed)
    if end_date is None:
        end_date = datetime.now(timezone.utc).date()

    if persona not in DEMO_PERSONAS:
        raise ValueError(f"Unknown demo persona '{persona}'. Choose from: {', '.join(DEMO_PERSONAS)}")
    profile = DEMO_PERSONAS[persona]

    # Pre-populate project name cache so render uses friendly demo names.
    for path, name in profile["project_dirs"].items():
        _project_name_cache[path] = name

    project_paths = list(profile["project_dirs"].keys())
    categories = profile["categories"]
    cat_weights = profile["weights"]
    persona_labels = profile["labels"]
    persona_tool_profiles = profile["tool_profiles"]

    sessions = []
    for i in range(n_sessions):
        category = rng.choices(categories, weights=cat_weights)[0]
        label = rng.choice(persona_labels[category])

        # Date — concentrated in the last 14 days, tail back to 32.
        days_back = int(min(31, max(0, abs(rng.gauss(8, 8)))))
        if rng.random() < 0.15:  # weekend bias — skip occasionally
            days_back += 1
        sess_date = end_date - timedelta(days=days_back)

        # Long session ~12% of the time.
        is_long = rng.random() < 0.12
        n_assistant = rng.randint(50, 200) if is_long else rng.randint(2, 35)
        n_user = max(1, n_assistant // rng.randint(2, 4))

        # Cost — proportional to length, with multipliers for long/heavy sessions.
        base = n_assistant * rng.uniform(0.04, 0.18)
        if is_long: base *= rng.uniform(1.5, 2.8)
        cost_target = max(0.05, base)

        # Token mix — cache reads dominate (the central insight of the tool).
        cache_read = int(cost_target * 100_000 * rng.uniform(2.5, 7.5))
        cache_write = int(cost_target * 100_000 * rng.uniform(0.4, 0.8))
        output_tok = int(rng.randint(120, 700) * n_assistant)
        input_tok = int(rng.randint(15, 180) * n_user)

        # Model — Opus heavy, but task-fit varies.
        model = rng.choices(
            ["claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
            weights=[55, 25, 18, 2]
        )[0]

        project_dir = rng.choice(project_paths)

        # Tool calls per category
        tool_calls = defaultdict(int)
        cat_tool_profile = persona_tool_profiles.get(category, {})
        for tool, (lo, hi) in cat_tool_profile.items():
            if hi > 0:
                tool_calls[tool] = rng.randint(lo, hi)

        # Build session record with same shape as the real parser.
        sess = _new_session()
        sess["session_id"] = f"demo-{i:04d}-{rng.randint(1000,9999):04x}"
        sess["project_dir"] = project_dir
        sess["label"] = label
        sess["category"] = category
        sess["first_prompt"] = label
        sess["messages_user"] = n_user
        sess["messages_assistant"] = n_assistant

        sess["tokens"]["input"] = input_tok
        sess["tokens"]["output"] = output_tok
        sess["tokens"]["cache_read"] = cache_read
        sess["tokens"]["cache_write_5m"] = cache_write
        sess["tokens"]["cache_write_1h"] = 0
        sess["tokens"]["web_search_requests"] = 0

        # Compute costs the same way the real path does, so totals reconcile.
        p = get_pricing(model)
        sess["cost"]["input"] = (input_tok / 1e6) * p["input"]
        sess["cost"]["output"] = (output_tok / 1e6) * p["output"]
        sess["cost"]["cache_read"] = (cache_read / 1e6) * p["cache_read"]
        sess["cost"]["cache_write_5m"] = (cache_write / 1e6) * p["cache_write_5m"]
        sess["cost"]["cache_write_1h"] = 0
        sess["cost"]["web_search"] = 0
        sess["cost_total"] = sum(sess["cost"][k] for k in
            ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "web_search"))

        ttok_total = input_tok + output_tok + cache_read + cache_write
        sess["by_model_tokens"][model] = ttok_total
        sess["by_model_cost"][model] = sess["cost_total"]
        sess["tool_calls"] = tool_calls
        sess["reads"] = sum(c for t, c in tool_calls.items() if t in READ_TOOLS)
        sess["writes"] = sum(c for t, c in tool_calls.items() if t in WRITE_TOOLS)
        sess["mcp_calls"] = sum(c for t, c in tool_calls.items() if "mcp__" in t)

        date_str = str(sess_date)
        sess["by_date"][date_str] = {"tokens": ttok_total, "cost": sess["cost_total"], "msgs": n_user + n_assistant}
        sess["first_date"] = sess_date
        sess["last_date"] = sess_date
        ts = datetime.combine(sess_date, datetime.min.time(), timezone.utc) + timedelta(hours=rng.randint(8, 18))
        # Per-turn timestamps spaced 1-3 min apart so focused-minutes calc gets a realistic value.
        turn_gaps = [rng.uniform(0.7, 2.5) for _ in range(n_assistant)]
        cumulative = 0.0
        turn_timestamps = []
        for g in turn_gaps:
            cumulative += g
            turn_timestamps.append(ts + timedelta(minutes=cumulative))
        sess["timestamps"] = [ts] + turn_timestamps
        sess["span_min"] = cumulative
        # focused minutes — same as cumulative since gaps are all ≤ 5 min by construction
        sess["duration_min"] = sum(g for g in turn_gaps if 0 < g <= 5)

        # Per-turn cost — power-law spread; small fraction take big share.
        avg_turn_cost = sess["cost_total"] / max(n_assistant, 1)
        per_turn = []
        cache_read_per_turn = cache_read // max(n_assistant, 1)
        cache_write_per_turn = cache_write // max(n_assistant, 1)
        for j in range(n_assistant):
            multiplier = rng.choices([0.3, 0.7, 1.0, 1.5, 3.0], weights=[20, 35, 25, 15, 5])[0]
            tc = avg_turn_cost * multiplier
            tool = rng.choice(list(tool_calls.keys()) + ["(text-only)", "(text-only)"]) if tool_calls else "(text-only)"
            cr = int(cache_read_per_turn * (1 + (j / max(n_assistant, 1)) * 1.5))  # bloat-staircase pattern
            cw = int(cache_write_per_turn * rng.uniform(0.7, 1.3))
            ot = int(output_tok / max(n_assistant, 1) * rng.uniform(0.5, 1.5))
            driver = "cache_read" if cr > cw * 2 else "cache_write" if cw > ot * 5 else "output"
            per_turn.append({
                "idx": j + 1, "cost": tc, "model": model,
                "tool": tool,
                "ts": (ts + timedelta(minutes=j * 1.5)).isoformat(),
                "session_id": sess["session_id"],
                "cache_read": cr, "output": ot, "cache_write": cw, "driver": driver,
            })
        sess["per_turn_cost"] = per_turn
        sess["per_turn_cache_read"] = [(t["idx"], t["cache_read"]) for t in per_turn]
        sess["avg_cache_read_per_turn"] = sum(t["cache_read"] for t in per_turn) / max(len(per_turn), 1)
        sess["max_cache_read_per_turn"] = max((t["cache_read"] for t in per_turn), default=0)

        # Flags — sprinkled realistically.
        flags = []
        if sess["avg_cache_read_per_turn"] > 300_000:
            flags.append("stale-context")
        if sess["reads"] > 5 and sess["writes"] == 0 and rng.random() < 0.6:
            flags.append("bulk-read-no-output")
        if rng.random() < 0.04:
            flags.append("redundant-reads")
        if rng.random() < 0.03:
            flags.append("low-interaction")
        if rng.random() < 0.02:
            flags.append("truncated-output")
        sess["flags"] = flags

        # Prompts
        sess["total_prompts"] = n_user
        sess["short_prompts"] = int(n_user * rng.uniform(0, 0.20))
        sess["detailed_prompts"] = int(n_user * rng.uniform(0.10, 0.40))
        sess["short_prompt_pct"] = sess["short_prompts"] / max(n_user, 1)

        sess["interrupts"] = rng.choices([0, 0, 0, 1, 2], weights=[60, 20, 10, 7, 3])[0]
        sess["redundant_reads"] = rng.choices([0, 0, 0, 1, 3, 7], weights=[70, 12, 8, 5, 3, 2])[0]
        sess["max_agent_streak"] = rng.randint(1, 9)
        sess["truncated_turns"] = 1 if "truncated-output" in flags else 0
        sess["subagent_spawns"] = rng.choices([0, 0, 1, 2], weights=[60, 25, 10, 5])[0]
        sess["skill_invocations"] = rng.randint(0, 3) if category == "PM Work" else (1 if rng.random() < 0.05 else 0)
        sess["skills_used"] = defaultdict(int)
        if sess["skill_invocations"] > 0:
            for _ in range(sess["skill_invocations"]):
                skill = rng.choice(["morning-sync", "digest-meeting", "debrief-thread", "weekly-status", "review-pr"])
                sess["skills_used"][skill] += 1
        sess["plan_mode_uses"] = rng.choices([0, 0, 0, 1, 2], weights=[70, 15, 8, 5, 2])[0]
        sess["sidechain_cost"] = 0
        sess["sidechain_tokens"] = 0
        sess["mcp_tools"] = defaultdict(int, {t: c for t, c in tool_calls.items() if "mcp__" in t})
        sess["mcp_tokens"] = sess["mcp_calls"] * rng.randint(2000, 25000)
        sess["mcp_tool_tokens"] = defaultdict(int, {t: c * rng.randint(2000, 30000) for t, c in tool_calls.items() if "mcp__" in t})
        sess["read_signatures"] = defaultdict(int)
        sess["opus_total_tokens"] = ttok_total if "opus" in model else 0
        sess["opus_trivial_tokens"] = int(ttok_total * rng.uniform(0.05, 0.30)) if "opus" in model else 0

        sessions.append(sess)

    return sessions


# ── HTML ─────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Efficiency Analyzer</title>
<style>
:root{--bg:#f5f6f8;--s1:#fff;--s2:#f0f1f4;--s3:#e5e7ee;--border:#dde0e8;--text:#1a1d2b;--muted:#6b7085;--dim:#9298b0;--accent:#6366f1;--green:#16a34a;--green-dim:rgba(22,163,74,.08);--yellow:#ca8a04;--yellow-dim:rgba(202,138,4,.08);--orange:#ea580c;--orange-dim:rgba(234,88,12,.08);--red:#dc2626;--blue:#2563eb;--blue-dim:rgba(37,99,235,.07);--purple:#a855f7}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;min-height:100vh}
.wrap{max-width:1140px;margin:0 auto;padding:28px 24px 60px}
.header{margin-bottom:14px}.header h1{font-size:22px;font-weight:700;margin-bottom:2px}.header p{font-size:13px;color:var(--muted)}
.header-row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.header-actions{display:flex;gap:8px;align-items:center}
.banner{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:12px 16px;margin-bottom:14px;display:flex;justify-content:space-between;align-items:flex-start;gap:14px;font-size:12px}
.banner h4{font-size:13px;font-weight:700;color:#9a3412;margin-bottom:3px}
.banner p{color:#7c2d12}
.banner button{background:transparent;border:none;color:#9a3412;font-size:18px;cursor:pointer;line-height:1;font-family:inherit;padding:0 4px}
.banner-chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#9a3412;background:#fff7ed;border:1px solid #fed7aa;padding:5px 10px;border-radius:6px;cursor:pointer;font-family:inherit}
.banner-chip:hover{background:#ffedd5}
.avail{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin:10px 0 6px;font-size:12px;color:var(--muted)}.avail strong{color:var(--text)}
.controls{display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap}
.dw input[type="date"]{background:var(--s1);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;font-family:inherit;cursor:pointer;min-width:150px}
.dw input:focus{outline:none;border-color:var(--accent)}
button{padding:8px 20px;border-radius:6px;font-size:13px;cursor:pointer;border:none;font-family:inherit;font-weight:600;transition:all .15s}
.bp{background:var(--accent);color:#fff}.bp:hover{background:#4f46e5}
.bg{background:var(--s1);color:var(--muted);border:1px solid var(--border)}.bg:hover{color:var(--text);background:var(--s2)}.bg.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.sep{color:var(--dim);font-size:13px}
#status{font-size:12px;color:var(--muted);margin-left:8px}

/* TL;DR hero */
.hero{background:linear-gradient(135deg,#eef2ff,#f5f3ff);border:1px solid #c7d2fe;border-radius:14px;padding:24px;margin-bottom:20px;box-shadow:0 2px 12px rgba(99,102,241,.08)}
.hero-headline{font-size:22px;font-weight:700;color:var(--text);margin-bottom:8px;line-height:1.3}
.hero-action{font-size:14px;color:#3730a3;background:#fff;padding:10px 14px;border-radius:8px;border:1px solid #c7d2fe;margin-bottom:14px;line-height:1.5}
.hero-action strong{color:#1e1b4b}
.hero-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:8px}
.hero-stat{text-align:left}
.hero-stat .v{font-size:22px;font-weight:800;color:var(--text);line-height:1.1}
.hero-stat .l{font-size:11px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.4px;font-weight:600}
.hero-stat .s{font-size:11px;color:var(--dim);margin-top:3px}
.hero-profile{font-size:12px;color:var(--muted);margin-top:10px;border-top:1px solid #c7d2fe;padding-top:10px}
.hero-profile strong{color:var(--text)}

.color-key{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted);background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:18px}
.color-key .key-item{display:inline-flex;align-items:center;gap:5px}
.color-key .ck{width:9px;height:9px;border-radius:2px}

.section-h{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;margin:26px 0 10px 0}

.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.card{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:16px}
.card .v{font-size:22px;font-weight:800;line-height:1.1}.card .l{font-size:11px;color:var(--muted);margin-top:3px}.card .s{font-size:10px;color:var(--dim);margin-top:6px;padding-top:6px;border-top:1px solid var(--border)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.tri-cols{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}
.panel{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:20px}
.panel.primary{border-color:#fed7aa;background:#fffbf5}
.panel h3{font-size:14px;font-weight:600;margin-bottom:4px}
.panel-sub{font-size:11px;color:var(--dim);margin-bottom:14px}

.target{margin-bottom:14px}
.target-h{display:flex;justify-content:space-between;align-items:baseline;font-size:12px;margin-bottom:5px}
.target-h .lbl{font-weight:600}
.target-h .val{color:var(--muted)}
.target-bar{height:8px;background:var(--s3);border-radius:4px;overflow:hidden;position:relative}
.target-fill{height:100%;border-radius:4px;transition:width .6s ease}
.target-marker{position:absolute;top:-3px;width:2px;height:14px;background:var(--text);opacity:.4}
.target-tip{font-size:10px;color:var(--dim);margin-top:4px}

.rec{padding:11px 14px;border-radius:8px;margin-bottom:7px;border-left:3px solid;font-size:12px}
.rec-good{background:var(--green-dim);border-color:var(--green)}.rec-warn{background:var(--yellow-dim);border-color:var(--yellow)}.rec-info{background:var(--blue-dim);border-color:var(--blue)}.rec-primary{background:var(--orange-dim);border-color:var(--orange)}
.rec h4{font-size:12px;font-weight:700;margin-bottom:3px}.rec p{font-size:11px;color:var(--muted);line-height:1.5}
.rec-good h4{color:var(--green)}.rec-warn h4{color:var(--yellow)}.rec-info h4{color:var(--blue)}.rec-primary h4{color:var(--orange)}
.rec .savings{display:inline-block;background:#fff;border:1px solid currentColor;color:inherit;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;margin-left:6px}

.gr{display:flex;align-items:center;gap:12px;margin-bottom:12px}.gl{width:110px;font-size:11px;color:var(--muted);text-align:right;flex-shrink:0}
.gt{flex:1;height:7px;background:var(--s3);border-radius:4px;overflow:hidden}.gf{height:100%;border-radius:4px;transition:width .6s ease}.gv{width:65px;font-size:12px;font-weight:600}
.br{display:flex;align-items:center;gap:8px;margin-bottom:5px}.bl{width:140px;font-size:11px;color:var(--muted);text-align:left;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bt{flex:1;height:20px;background:var(--s3);border-radius:3px;overflow:hidden}.bf{height:100%;border-radius:3px;display:flex;align-items:center;padding-left:6px;font-size:9px;font-weight:600;color:#fff;transition:width .5s ease;min-width:2px}
.bv{width:90px;font-size:10px;text-align:right;color:var(--muted);flex-shrink:0}

.tt{width:100%;border-collapse:collapse;font-size:11px}
.tt th{padding:6px 8px;color:var(--muted);font-weight:600;text-align:left;border-bottom:2px solid var(--border)}
.tt td{padding:8px;border-bottom:1px solid var(--border)}
.tt tr.clickable{cursor:pointer;transition:background .15s}
.tt tr.clickable:hover{background:var(--s2)}
.session-link{color:var(--accent);cursor:pointer;text-decoration:none;border-bottom:1px dashed var(--accent)}
.session-link:hover{color:#4f46e5}

.spark{display:flex;align-items:flex-end;height:42px;gap:1px;width:100%}
.spark .b{flex:1;background:var(--blue);border-radius:1px;min-height:1px;cursor:pointer}
.spark .b:hover{background:var(--accent)}

.cat-pill{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;background:var(--s2);color:var(--muted);margin-right:4px}
.cat-coding{background:#dbeafe;color:#1e40af}
.cat-data-analysis{background:#dcfce7;color:#166534}
.cat-pm-work{background:#fef3c7;color:#92400e}
.cat-design-work{background:#fce7f3;color:#9d174d}
.cat-debugging{background:#fee2e2;color:#991b1b}
.cat-research{background:#ede9fe;color:#5b21b6}
.cat-writing{background:#fff1e6;color:#9a3412}

.tr{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--border)}.tr:last-child{border-bottom:none}
.tm{width:110px;font-size:12px;color:var(--muted)}.tv{font-size:13px;flex:1}
.ta{font-size:11px;font-weight:600;padding:2px 8px;border-radius:4px}
.ta-ug{background:var(--green-dim);color:var(--green)}.ta-ub{background:var(--orange-dim);color:var(--orange)}.ta-dg{background:var(--green-dim);color:var(--green)}.ta-db{background:var(--orange-dim);color:var(--orange)}.ta-s{background:var(--blue-dim);color:var(--blue)}

.score-tile{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center;font-size:11px;color:var(--muted)}
.score-tile .num{font-size:28px;font-weight:800;line-height:1}
.score-tile .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.4px;font-weight:600;margin-top:4px}
.score-tile .desc{font-size:10px;margin-top:4px}

.miniStat{text-align:center;padding:0 8px}
.miniStat .v{font-size:18px;font-weight:700}
.miniStat .l{font-size:10px;color:var(--muted);margin-top:2px}

.empty-state{text-align:center;padding:60px 24px;background:var(--s1);border:1px solid var(--border);border-radius:12px;margin-bottom:20px}
.empty-state h3{font-size:16px;margin-bottom:8px}
.empty-state p{font-size:13px;color:var(--muted);max-width:520px;margin:0 auto 12px;line-height:1.5}

.modal-bg{position:fixed;inset:0;background:rgba(15,17,30,.45);display:none;z-index:100;align-items:flex-start;justify-content:center;padding:40px 20px;overflow-y:auto}
.modal-bg.open{display:flex}
.modal{background:var(--s1);border-radius:12px;max-width:900px;width:100%;padding:24px;box-shadow:0 16px 48px rgba(15,17,30,.18);position:relative}
.modal-close{position:absolute;top:12px;right:14px;background:transparent;border:none;font-size:22px;cursor:pointer;color:var(--muted);font-family:inherit}
.modal-close:hover{color:var(--text)}
.modal h2{font-size:16px;margin-bottom:4px;padding-right:30px}
.modal-sub{font-size:11px;color:var(--muted);margin-bottom:16px}

.loading{text-align:center;padding:60px 0;color:var(--muted);font-size:14px}.hidden{display:none}
.footer{text-align:center;padding:24px 0;font-size:11px;color:var(--dim)}.footer a{color:var(--accent);text-decoration:none}.footer code{background:var(--s2);padding:1px 5px;border-radius:3px;font-size:10px}

@media(max-width:900px){.tri-cols{grid-template-columns:1fr};.hero-stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header"><div class="header-row">
    <div><h1>Claude Code Efficiency Analyzer</h1><p>How you actually use Claude Code. Local-only, no data leaves your machine.</p></div>
    <div class="header-actions">
      <button id="bannerChip" class="banner-chip hidden" onclick="toggleBannerExpand()" title="API-equivalent cost — what about Pro/Max?">Pro / Max?</button>
    </div>
  </div></div>
  <div id="banner" class="hidden"></div>
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
  <div class="footer">Reads <code>~/.claude/projects/</code> only. Cost estimates use <a href="https://platform.claude.com/docs/en/about-claude/pricing" target="_blank">Anthropic published API pricing</a>.</div>
</div>
<div id="modalBg" class="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal"></div>
</div>
<script>
const $=s=>document.querySelector(s);let AV=null;

async function init(){
  try{const r=await fetch('/api/availability');AV=await r.json();
    if(!AV.first_date){$('#avail').innerHTML='No Claude Code session logs found in <code>~/.claude/projects/</code>. Use Claude Code first, then come back.';return}
    const n=AV.active_dates.length;const span=Math.round((new Date(AV.last_date)-new Date(AV.first_date))/864e5)+1;
    const ret=AV.retention||{};
    const retNote=ret.days?(ret.is_default
      ?` Claude Code keeps the last <strong>${ret.days} days</strong> locally by default — older sessions auto-rotate. Bump <code>cleanupPeriodDays</code> in <code>~/.claude/settings.json</code> if you want a longer history.`
      :` Your <code>cleanupPeriodDays</code> is set to <strong>${ret.days} days</strong>. Older sessions auto-rotate.`):'';
    $('#avail').innerHTML=`Logs cover <strong>${AV.first_date}</strong> to <strong>${AV.last_date}</strong> (${n} active days across ${span} calendar days, ${AV.total_files} session files).${retNote}`;
    $('#df').min=AV.first_date;$('#df').max=AV.last_date;$('#dt').min=AV.first_date;$('#dt').max=AV.last_date;$('#dt').value=AV.last_date;
    preset(30)
  }catch(e){$('#avail').textContent='Error: '+e.message}
}
function preset(days){document.querySelectorAll('.bg').forEach(b=>b.classList.remove('active'));if(event&&event.target&&event.target.classList)event.target.classList.add('active');if(!AV||!AV.first_date)return;if(days===0){$('#df').value=AV.first_date;$('#dt').value=AV.last_date}else{const d=new Date(AV.last_date);d.setDate(d.getDate()-days);const m=new Date(AV.first_date);$('#df').value=(d<m?AV.first_date:d.toISOString().slice(0,10));$('#dt').value=AV.last_date}run()}
async function run(){
  const from=$('#df').value,to=$('#dt').value;if(!from||!to)return;
  CATEGORY_FILTER=null;  // reset when re-running with new dates
  $('#status').textContent='Scanning...';$('#loading').classList.remove('hidden');$('#loading').textContent='Analyzing sessions...';$('#dash').classList.add('hidden');
  try{const url=`/api/analyze?from=${from}&to=${to}`;
    const r=await fetch(url);const d=await r.json();
    if(d.error){$('#loading').textContent=d.error;$('#status').textContent='';return}
    render(d);$('#dash').classList.remove('hidden');$('#loading').classList.add('hidden');$('#status').textContent=d.summary.sessions+' sessions';
    maybeShowBanner(d);
  }catch(e){$('#loading').textContent='Error: '+e.message;$('#status').textContent=''}
}

function maybeShowBanner(d){
  // Default state: a small "Pro / Max?" chip in the header. Clicking it
  // expands the full explainer. Once the user dismisses the expanded version,
  // the chip stays so they can re-open it later — no permanent dismissal.
  const chip=$('#bannerChip');if(chip)chip.classList.remove('hidden');
}
function toggleBannerExpand(){
  const b=$('#banner');
  if(b.classList.contains('hidden')){
    b.classList.remove('hidden');
    b.className='banner';
    b.innerHTML=`<div><h4>On Claude Pro or Max?</h4><p>The dollar amounts below are <strong>API-equivalent</strong> — what your usage would cost at API rates. Pro ($20/mo) and Max ($100-200/mo) are flat-fee, so this is <em>not</em> your actual bill. Useful for comparing across periods or spotting expensive patterns, not for predicting your invoice.</p></div><button onclick="toggleBannerExpand()" title="Collapse">×</button>`;
  }else{b.classList.add('hidden')}
}
function dismissBanner(){localStorage.setItem(BANNER_KEY,'1');$('#banner').classList.add('hidden')}

function fmt(n){return n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(Math.round(n))}
function fC(n){return n>=1?'$'+n.toFixed(2):n>=.01?'$'+n.toFixed(3):'$'+n.toFixed(4)}
function pct(n){return(n*100).toFixed(1)+'%'}
function pctR(n){return Math.round(n*100)+'%'}
function sc(s){return s>=75?'var(--green)':s>=55?'var(--blue)':s>=35?'var(--yellow)':s>=20?'var(--orange)':'var(--red)'}
function catClass(c){return 'cat-'+(c||'other').toLowerCase().replace(/[^a-z]/g,'-').replace(/--+/g,'-')}
function escHtml(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function rR(arr,kind){if(!arr||!arr.length)return'';return arr.map(r=>`<div class="rec rec-${kind}"><h4>${escHtml(r.title)}${r.savings?`<span class="savings">~${fC(r.savings)}</span>`:''}</h4><p>${r.body}</p></div>`).join('')}

// ── Hero ──
// Action-first: lead with what to do, then the supporting fact, then numbers.
function renderHero(d){
  const s=d.summary,e=d.efficiency,sc_=d.scores,tf=d.top_finding;
  const fact=d.hero_headline||`${fC(s.total_cost)} across ${s.sessions} sessions.`;
  const headline=tf?`${escHtml(tf.headline)}.`:'Your usage looks balanced — no single action stands out.';
  const sub=tf?`<strong>Do this:</strong> ${escHtml(tf.action)} <span style="color:var(--dim)">·</span> <span style="color:var(--muted)">${escHtml(fact)}</span>`:`<span style="color:var(--muted)">${escHtml(fact)}</span>`;
  let h=`<div class="hero">
    <div class="hero-headline">${headline}</div>
    <div class="hero-action">${sub}</div>
    <div class="hero-stats">
      <div class="hero-stat"><div class="v">${fC(s.total_cost)}</div><div class="l">Period spend</div><div class="s">~${fC(e.projected_monthly)}/mo · ${fC(e.projected_yearly)}/yr</div></div>
      <div class="hero-stat"><div class="v">${s.total_hours}h</div><div class="l">Active hours</div><div class="s">${s.sessions} sessions · ${s.total_user_msgs+s.total_asst_msgs} messages</div></div>
      <div class="hero-stat"><div class="v">${pctR(e.cache_hit_rate)}</div><div class="l">Cache reuse</div><div class="s">${fmt(s.total_tokens)} tokens processed</div></div>
      <div class="hero-stat"><div class="v">${d.period.active_days} of ${d.period.days}</div><div class="l">Days active</div><div class="s">${(s.total_user_msgs+s.total_asst_msgs)/Math.max(d.period.active_days,1)|0} msgs/active day</div></div>
    </div>
    <div class="hero-profile"><strong>${escHtml(sc_.usage_band)}</strong> usage · <strong>${escHtml(sc_.efficiency_band)}</strong> efficiency</div>
  </div>`;
  return h;
}

// ── Color key ──
function renderColorKey(){
  return `<div class="color-key">
    <span class="key-item"><span class="ck" style="background:var(--green)"></span> Going well</span>
    <span class="key-item"><span class="ck" style="background:var(--orange)"></span> Top opportunity</span>
    <span class="key-item"><span class="ck" style="background:var(--yellow)"></span> Worth checking</span>
    <span class="key-item"><span class="ck" style="background:var(--blue)"></span> Informational</span>
    <span class="key-item" style="margin-left:auto;color:var(--dim)">Click any session label for a turn-by-turn breakdown</span>
  </div>`;
}

// ── Targets ──
function renderTargets(targets){
  if(!targets||!targets.length)return'';
  let h='<div class="panel" style="margin-bottom:20px"><h3>Goals</h3><p class="panel-sub">Where you stand on a few key metrics. The vertical line is the target.</p>';
  for(const t of targets){
    let val=t.value,target=t.target,fillW=0,markerW=0,disp,targetDisp,onTrack=false;
    if(t.fmt==='pct'){val=Math.min(t.value,1);target=Math.min(t.target,1);fillW=val*100;markerW=target*100;disp=pctR(t.value);targetDisp='≥'+pctR(t.target);onTrack=t.value>=t.target}
    else if(t.fmt==='pct_inv'){val=Math.min(t.value,1);target=Math.min(t.target,1);fillW=val*100;markerW=target*100;disp=pctR(t.value);targetDisp='≤'+pctR(t.target);onTrack=t.value<=t.target}
    else if(t.fmt==='days'){const cap=7;fillW=Math.min(t.value/cap,1)*100;markerW=Math.min(t.target/cap,1)*100;disp=t.value.toFixed(1)+'d/wk';targetDisp='≥'+t.target+'d/wk';onTrack=t.value>=t.target}
    else if(t.fmt==='msgs'){const cap=Math.max(t.target*2,30);fillW=Math.min(t.value/cap,1)*100;markerW=Math.min(t.target/cap,1)*100;disp=t.value.toFixed(0);targetDisp='≥'+t.target;onTrack=t.value>=t.target}
    const color=onTrack?'var(--green)':'var(--orange)';
    h+=`<div class="target"><div class="target-h"><div class="lbl">${escHtml(t.name)} <span style="color:${color}">${onTrack?'✓':'·'}</span></div><div class="val">${disp} <span style="color:var(--dim);margin-left:6px">target ${targetDisp}</span></div></div><div class="target-bar"><div class="target-fill" style="width:${fillW}%;background:${color}"></div><div class="target-marker" style="left:${markerW}%"></div></div><div class="target-tip">${escHtml(t.tip)}</div></div>`;
  }
  return h+'</div>';
}

// ── Recs ──
function renderRecs(R){
  const sortBySavings=(a,b)=>(b.savings||0)-(a.savings||0);
  const primary=(R.primary||[]).slice().sort(sortBySavings);
  const secondary=(R.secondary||[]).slice().sort(sortBySavings);
  const good=(R.good||[]);
  const PRIMARY_CAP=3,SECONDARY_CAP=3;
  const primaryTop=primary.slice(0,PRIMARY_CAP),primaryRest=primary.slice(PRIMARY_CAP);
  const secondaryTop=secondary.slice(0,SECONDARY_CAP),secondaryRest=secondary.slice(SECONDARY_CAP);
  let h='<div class="cols">';
  h+='<div class="panel"><h3>Where to focus</h3><p class="panel-sub">Top opportunities, ranked by estimated savings</p>';
  h+=primaryTop.length?rR(primaryTop,'primary'):'<p style="font-size:12px;color:var(--muted)">No major issues. See smaller items below.</p>';
  if(secondaryTop.length){
    h+='<div style="margin-top:14px;font-size:11px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Smaller items</div>';
    h+=rR(secondaryTop,'info');
  }
  const restCount=primaryRest.length+secondaryRest.length;
  if(restCount>0){
    h+=`<details style="margin-top:10px"><summary style="cursor:pointer;font-size:11px;color:var(--muted);font-weight:600">+ ${restCount} more</summary><div style="margin-top:8px">`;
    h+=rR(primaryRest,'primary');
    h+=rR(secondaryRest,'info');
    h+='</div></details>';
  }
  h+='</div>';
  h+='<div class="panel"><h3>Going well</h3><p class="panel-sub">Habits worth keeping</p>';
  h+=good.length?rR(good,'good'):'<p style="font-size:12px;color:var(--muted)">No strong positive signals yet — use Claude more to build a pattern.</p>';
  h+='</div></div>';
  return h;
}

// ── Money ──
function renderMoney(d){
  const cb=d.cost_breakdown||{};
  const eff=d.efficiency||{};
  const cw=(cb.cache_write_5m||0)+(cb.cache_write_1h||0);
  const total=(cb.input||0)+(cb.output||0)+(cb.cache_read||0)+cw+(cb.web_search||0);
  if(total<=0)return'';
  const pI=(cb.input/total)*100,pO=(cb.output/total)*100,pCR=(cb.cache_read/total)*100,pCW=(cw/total)*100,pWS=((cb.web_search||0)/total)*100;
  const cacheTotal=(cb.cache_read||0)+cw;
  const cachePct=pCR+pCW;
  const hitPct=((eff.cache_hit_rate||0)*100).toFixed(0);
  const avgMsgs=Math.round(eff.msgs_per_session||0);

  let h='<div class="panel" style="margin-bottom:20px"><h3>Where the money went</h3>';

  // Lead with the aha if cache dominates spend (which is true for nearly all real users).
  if(cachePct>50){
    h+=`<div style="background:var(--blue-dim);border-left:3px solid var(--blue);padding:14px 16px;border-radius:6px;margin-bottom:18px">`;
    h+=`<div style="font-size:17px;font-weight:600;line-height:1.4;margin-bottom:8px">${fC(cacheTotal)} of ${fC(total)} (${cachePct.toFixed(0)}%) went to re-reading your conversation history.</div>`;
    h+=`<p style="font-size:13px;line-height:1.55;color:var(--ink);margin:0">Every Claude Code turn re-loads the entire conversation: every prior message, file, and tool result. Your sessions average <strong>${avgMsgs} messages</strong>, so by turn ${avgMsgs} Claude is rereading turns 1 through ${Math.max(avgMsgs-1,1)}. The cache itself is doing its job (<strong>${hitPct}% hit rate</strong> means 96 of every 100 input tokens are reused, not freshly typed). Cache <em>cost</em> is high because your sessions are long, not because anything is broken.</p>`;
    h+=`<p style="font-size:13px;line-height:1.55;color:var(--ink);margin:8px 0 0"><strong>The lever:</strong> Use <code>/clear</code> when you switch tasks mid-session, or start a fresh session for unrelated work. Shorter sessions, less re-reading.</p>`;
    h+='</div>';
  }

  h+='<p class="panel-sub" style="margin-bottom:8px">Full split by token type</p>';
  h+='<div style="display:flex;height:32px;border-radius:6px;overflow:hidden;margin-bottom:16px">';
  if(pCR>1)h+=`<div style="width:${pCR}%;background:#3b82f6;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Cache reads: ${fC(cb.cache_read)}">${pCR>8?'Cache reads':''}</div>`;
  if(pCW>1)h+=`<div style="width:${pCW}%;background:#f97316;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Cache writes: ${fC(cw)}">${pCW>8?'Cache writes':''}</div>`;
  if(pO>1)h+=`<div style="width:${pO}%;background:#16a34a;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Output: ${fC(cb.output)}">${pO>5?'Output':''}</div>`;
  if(pI>1)h+=`<div style="width:${pI}%;background:#6366f1;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Input: ${fC(cb.input)}">${pI>5?'Input':''}</div>`;
  if(pWS>0.5)h+=`<div style="width:${pWS}%;background:#a855f7;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff" title="Web search: ${fC(cb.web_search)}">${pWS>5?'Web':''}</div>`;
  h+='</div><div style="display:flex;gap:24px;flex-wrap:wrap">';
  h+=cl('#3b82f6','Cache reads',cb.cache_read,pCR,'Re-reading conversation history each turn');
  h+=cl('#f97316','Cache writes',cw,pCW,'Loading new files, tool results, system context');
  h+=cl('#16a34a','Output',cb.output,pO,"Claude's responses to you");
  h+=cl('#6366f1','Input',cb.input,pI,'Your prompts and instructions');
  if((cb.web_search||0)>0)h+=cl('#a855f7','Web search',cb.web_search,pWS,'$10 per 1,000 web_search calls');
  h+='</div>';
  return h+'</div>';
}

function cl(color,label,cost,p,desc){return`<div style="display:flex;align-items:flex-start;gap:6px"><div style="width:10px;height:10px;border-radius:2px;background:${color};margin-top:3px;flex-shrink:0"></div><div><div style="font-size:12px"><span style="color:var(--muted)">${label}</span> <strong>${fC(cost)}</strong> <span style="font-size:11px;color:var(--dim)">(${p.toFixed(0)}%)</span></div><div style="font-size:10px;color:var(--dim)">${desc}</div></div></div>`}

// ── External services (split panel) ──
function renderExternal(sh){
  if(!sh.mcp_heavy_tools||!sh.mcp_heavy_tools.length)return'';
  let h='<div class="panel"><h3>External services</h3><p class="panel-sub">Estimated tokens consumed by data returned from each MCP tool</p>';
  const mx=sh.mcp_heavy_tools[0].tokens;
  for(const t of sh.mcp_heavy_tools){
    const tok=t.tokens,label=tok>=1e3?((tok/1e3).toFixed(0)+'K'):String(tok);
    const barW=Math.max((tok/mx)*100,8);
    h+=`<div class="br"><div class="bl" title="${escHtml(t.name)}">${escHtml(t.friendly)}</div><div class="bt"><div class="bf" style="width:${barW}%;background:var(--orange)">${barW>30?label:''}</div></div><div class="bv">${label} ~${fC(t.est_cost)}</div></div>`;
  }
  return h+'<p style="font-size:11px;color:var(--dim);margin-top:8px">Token cost is estimated using a conservative cache-write rate. Actual cost depends on the model used.</p></div>';
}

// ── Models (split panel) ──
function renderModels(d){
  if(!d.models||!d.models.length)return'';
  let h='<div class="panel"><h3>Models used</h3><p class="panel-sub">Token share and cost by model</p>';
  const mx=d.models[0].tokens;
  for(const m of d.models){if(!m.tokens)continue;
    const c=m.model.includes('opus')?'var(--orange)':m.model.includes('haiku')?'var(--green)':'var(--blue)';
    h+=`<div class="br"><div class="bl" title="${escHtml(m.model)}">${escHtml(m.friendly)}</div><div class="bt"><div class="bf" style="width:${(m.tokens/mx)*100}%;background:${c}">${pct(m.pct)}</div></div><div class="bv">${fC(m.cost)}</div></div>`;
  }
  return h+'</div>';
}

// ── Activity ──
function renderActivity(d){
  if(!d.tools||!d.tools.length)return'';
  const cats={explore:0,create:0,mcp:0,other:0};
  const eN=['Read','Grep','Glob','Bash','WebSearch','WebFetch'],cN=['Edit','Write','NotebookEdit'];
  for(const t of d.tools){if(eN.includes(t.raw))cats.explore+=t.count;else if(cN.includes(t.raw))cats.create+=t.count;else if(t.raw&&t.raw.startsWith('mcp__'))cats.mcp+=t.count;else cats.other+=t.count}
  const total=cats.explore+cats.create+cats.mcp+cats.other;
  const pe=(cats.explore/total)*100,pc=(cats.create/total)*100,pm=(cats.mcp/total)*100,po=(cats.other/total)*100;
  let h='<div class="panel"><h3>What Claude was doing</h3><p class="panel-sub">Tool calls grouped by activity type</p>';
  h+='<div style="display:flex;height:28px;border-radius:6px;overflow:hidden;margin-bottom:16px">';
  if(pe>1)h+=`<div style="width:${pe}%;background:var(--blue);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${pe>10?'Reading':''}</div>`;
  if(pc>1)h+=`<div style="width:${pc}%;background:var(--green);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${pc>10?'Writing':''}</div>`;
  if(pm>1)h+=`<div style="width:${pm}%;background:var(--orange);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${pm>8?'External':''}</div>`;
  if(po>1)h+=`<div style="width:${po}%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${po>8?'Other':''}</div>`;
  h+='</div><div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px">';
  h+=al('var(--blue)','Reading & searching',cats.explore,pe);
  h+=al('var(--green)','Writing & editing',cats.create,pc);
  h+=al('var(--orange)','External services',cats.mcp,pm);
  h+=al('var(--accent)','Other',cats.other,po);
  h+='</div>';
  const rwR=cats.explore>0&&cats.create>0?(cats.explore/cats.create).toFixed(1):'N/A';
  h+=`<p style="font-size:11px;color:var(--muted)">For every file edited, Claude read about <strong>${rwR}</strong> first.</p></div>`;
  return h;
}

function al(color,label,count,p){return`<div style="display:flex;align-items:flex-start;gap:6px"><div style="width:10px;height:10px;border-radius:2px;background:${color};margin-top:3px;flex-shrink:0"></div><div><div style="font-size:12px"><span style="color:var(--muted)">${label}</span> <strong>${count}</strong> <span style="font-size:11px;color:var(--dim)">(${p.toFixed(0)}%)</span></div></div></div>`}

// ── Categories ──
function renderCategories(cats){
  if(!cats||!cats.length||cats.length===1)return'';
  let h='<div class="panel" style="margin-bottom:20px"><h3>What you used Claude Code for</h3><p class="panel-sub">Sessions auto-categorized by intent</p>';
  const mx=cats[0].cost;
  for(const c of cats){
    const w=mx>0?Math.max((c.cost/mx)*100,3):0;
    h+=`<div class="br"><div class="bl"><span class="cat-pill ${catClass(c.name)}">${escHtml(c.name)}</span></div><div class="bt"><div class="bf" style="width:${w}%;background:var(--accent)">${w>20?fC(c.cost):''}</div></div><div class="bv">${c.sessions} sess · ${fC(c.cost)}</div></div>`;
  }
  return h+'</div>';
}

// ── Best moments ──
function renderBest(moments){
  if(!moments||!moments.length)return'';
  let h='<div class="panel" style="margin-bottom:20px"><h3>Best moments</h3><p class="panel-sub">Sessions worth celebrating</p>';
  for(const m of moments){
    const dur=Math.round(m.duration_min);
    const hStr=dur>=60?`${Math.floor(dur/60)}h ${dur%60}m`:`${dur}m`;
    h+=`<div style="padding:12px 0;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:600;color:var(--green);text-transform:uppercase;letter-spacing:.3px">${escHtml(m.title)}</div>
      <div style="display:flex;align-items:baseline;gap:12px;margin-top:6px;flex-wrap:wrap">
        <div style="font-size:18px;font-weight:700;color:var(--text)">${hStr}</div>
        <div style="font-size:13px;color:var(--muted)">${m.messages} messages · ${m.writes} writes</div>
      </div>
      <div style="font-size:13px;font-weight:600;margin-top:6px">${m.session_id?`<a class="session-link" onclick="openSession('${m.session_id}')">${escHtml(m.label)}</a>`:escHtml(m.label)}</div>
      <div style="font-size:11px;color:var(--dim);margin-top:4px">
        <span class="cat-pill ${catClass(m.category)}">${escHtml(m.category)}</span>
        ${fC(m.cost)} spent
      </div>
    </div>`;
  }
  return h+'</div>';
}

// ── Top expensive turns (with optional category filter) ──
let CATEGORY_FILTER=null;  // global filter state, mutated by category-pill clicks
let LAST_DATA=null;        // cached so we can re-render without refetching

function renderTopTurns(turns){
  if(!turns||!turns.length)return'';
  const driverFriendly={cache_read:'Cache read',cache_write:'Cache write',output:'Output',input:'Input',web_search:'Web search'};
  const filtered=CATEGORY_FILTER?turns.filter(t=>t.session_category===CATEGORY_FILTER).slice(0,10):turns;
  let h='<div class="panel" style="margin-bottom:20px" id="topTurnsPanel"><h3>Top 10 most expensive turns</h3><p class="panel-sub">Highest-cost single messages. Click a session to see its turn-by-turn breakdown.</p>';
  if(CATEGORY_FILTER){
    h+=`<div style="background:var(--blue-dim);color:var(--blue);padding:6px 12px;border-radius:6px;font-size:11px;margin-bottom:10px;display:inline-flex;align-items:center;gap:8px">Filtered to <strong>${escHtml(CATEGORY_FILTER)}</strong> <a onclick="clearTurnFilter()" style="cursor:pointer;text-decoration:underline">clear</a></div>`;
  }
  if(!filtered.length){
    h+=`<p style="font-size:12px;color:var(--muted)">No turns matched this filter.</p></div>`;
    return h;
  }
  // Dominance callout — when one session owns ≥5 of the top 10, that's the
  // story, not the table. Surface it explicitly.
  if(!CATEGORY_FILTER && filtered.length>=5){
    const counts={},costs={},labels={},sids={};
    for(const t of filtered.slice(0,10)){
      const k=t.session_id||t.session_label;
      counts[k]=(counts[k]||0)+1;
      costs[k]=(costs[k]||0)+t.cost;
      labels[k]=t.session_label;
      sids[k]=t.session_id;
    }
    let topK=null,topN=0;
    for(const k in counts){if(counts[k]>topN){topN=counts[k];topK=k}}
    if(topN>=5){
      const lbl=labels[topK]||'one session';
      const sid=sids[topK];
      const link=sid?`<a class="session-link" onclick="openSession('${sid}')">${escHtml(lbl)}</a>`:escHtml(lbl);
      h+=`<div style="background:var(--orange-dim);border-left:3px solid var(--orange);padding:10px 14px;border-radius:6px;font-size:12px;margin-bottom:14px;line-height:1.5"><strong>${topN} of your top 10 expensive turns came from one session</strong> (${link}, ${fC(costs[topK])} of these turns alone). Long sessions like this benefit most from <code>/clear</code> checkpoints when context is no longer load-bearing.</div>`;
    }
  }
  h+='<div style="overflow-x:auto"><table class="tt"><thead><tr><th>#</th><th>Cost</th><th>Driver</th><th>Tool</th><th>Model</th><th>Session</th><th>When</th></tr></thead><tbody>';
  filtered.forEach((t,i)=>{
    const when=t.ts?t.ts.slice(0,16).replace('T',' '):'';
    h+=`<tr><td>${i+1}</td><td style="font-weight:600">${fC(t.cost)}</td><td><span style="font-size:10px;color:var(--muted)">${driverFriendly[t.driver]||t.driver}</span></td><td>${escHtml(t.tool_friendly)}</td><td>${escHtml(t.model_friendly)}</td><td><a class="session-link" onclick="openSession('${t.session_id}')">${escHtml(t.session_label)}</a></td><td style="font-size:10px;color:var(--dim)">${when}</td></tr>`;
  });
  return h+'</tbody></table></div></div>';
}

function filterTurnsByCategory(cat){
  CATEGORY_FILTER=(CATEGORY_FILTER===cat)?null:cat;
  if(LAST_DATA){
    render(LAST_DATA);
    const panel=document.getElementById('topTurnsPanel');
    if(panel)panel.scrollIntoView({behavior:'smooth',block:'start'});
  }
}
function clearTurnFilter(){CATEGORY_FILTER=null;if(LAST_DATA)render(LAST_DATA)}

// ── Bloat ──
// Sparkline + an inline /clear suggestion when the curve crossed 250K cache
// reads. Below the spark we label every ~6th turn so readers can locate the
// bloat point without hovering 30 bars.
function renderBloat(curves){
  if(!curves||!curves.length)return'';
  let h='<div class="panel" style="margin-bottom:20px"><h3>Context bloat — top 3 sessions</h3><p class="panel-sub">Cache reads per turn over the life of each session. A rising staircase = stale context piling up.</p>';
  for(const c of curves){
    const mx=Math.max(...c.points.map(p=>p.cr),1);
    // First turn where cache read crossed 250K — a reasonable /clear hint
    const THRESH=250000;
    let crossIdx=null;
    for(const p of c.points){if(p.cr>=THRESH){crossIdx=p.idx;break}}
    const crossNote=crossIdx?`<span style="color:var(--orange);font-weight:600">Suggested <code>/clear</code> around turn ${crossIdx}</span> · `:'';
    h+=`<div style="margin-bottom:18px"><div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;gap:10px"><div style="font-size:12px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><a class="session-link" onclick="openSession('${c.session_id}')">${escHtml(c.label)}</a></div><div style="font-size:11px;color:var(--muted);flex-shrink:0">${c.messages} msgs · avg ${fmt(c.avg_cache_read)} cache · ${fC(c.cost)}</div></div><div class="spark">`;
    for(const p of c.points){const hp=Math.max((p.cr/mx)*100,1);h+=`<div class="b" style="height:${hp}%" title="turn ${p.idx}: ${fmt(p.cr)} cache read"></div>`}
    h+='</div>';
    // Axis labels every ~6th turn (max 6 labels across the row)
    const n=c.points.length;
    const step=Math.max(1,Math.floor(n/6));
    let labels='';
    for(let i=0;i<n;i+=step){labels+=`<span style="flex:${step}">t${c.points[i].idx}</span>`}
    h+=`<div style="display:flex;font-size:9px;color:var(--dim);margin-top:2px;letter-spacing:.3px">${labels}</div>`;
    h+=`<div style="font-size:10px;color:var(--muted);margin-top:4px">${crossNote}peak ${fmt(mx)} cache read on turn ${c.points.find(p=>p.cr===mx)?.idx||'?'}</div></div>`;
  }
  return h+'<p style="font-size:11px;color:var(--dim);margin-top:6px">When bars stop dropping back down between turns, the conversation has accumulated context that\'s no longer load-bearing. <code>/clear</code> resets it.</p></div>';
}

// ── Daily ──
function renderDaily(daily){
  if(!daily||!daily.length)return'';
  let h='<div class="panel" style="margin-bottom:20px"><h3>Daily spend</h3><p class="panel-sub">How much you spent each day</p>';
  const mx=Math.max(...daily.map(x=>x.cost),0.01);
  const show=daily.length>14?daily.slice(-14):daily;
  h+='<div style="display:flex;gap:14px;font-size:10px;color:var(--muted);margin-bottom:10px;flex-wrap:wrap">';
  h+='<span style="display:inline-flex;align-items:center;gap:5px"><span style="width:9px;height:9px;border-radius:2px;background:var(--green)"></span>under $30</span>';
  h+='<span style="display:inline-flex;align-items:center;gap:5px"><span style="width:9px;height:9px;border-radius:2px;background:var(--blue)"></span>$30–$100</span>';
  h+='<span style="display:inline-flex;align-items:center;gap:5px"><span style="width:9px;height:9px;border-radius:2px;background:var(--orange)"></span>over $100</span>';
  if(daily.length>14)h+=`<span style="margin-left:auto;color:var(--dim)">Showing last 14 days of ${daily.length}</span>`;
  h+='</div>';
  for(const day of show){const lb=day.date.slice(5);const w=mx>0?(day.cost/mx)*100:0;const c=day.cost>100?'var(--orange)':day.cost>30?'var(--blue)':day.cost>0?'var(--green)':'var(--dim)';h+=`<div class="br"><div class="bl">${lb}</div><div class="bt"><div class="bf" style="width:${w}%;background:${c}">${day.cost>=1?fC(day.cost):''}</div></div><div class="bv">${day.sessions}s · ${day.msgs}m</div></div>`}
  return h+'</div>';
}

// ── Health ──
const FLAG_LABELS={'bulk-read-no-output':'Heavy reading, no edits','mcp-heavy':'MCP-heavy','low-interaction':'Low interaction','high-interrupts':'High interrupts','deep-agent-loop':'Deep agent loop','redundant-reads':'Redundant file reads','stale-context':'Stale context','truncated-output':'Truncated output'};
function renderHealth(s,sh){
  let h='<div class="panel" style="margin-bottom:20px"><h3>Workflow quality signals</h3><p class="panel-sub">How sessions ran — clean, interrupted, scattered. Not a measure of output quality, just of how Claude Code got used.</p>';
  h+='<div style="display:flex;gap:18px;margin-bottom:8px;flex-wrap:wrap">';
  h+=ms('Clean sessions',sh.clean_pct+'%',sh.clean_pct>=90?'var(--green)':sh.clean_pct>=70?'var(--blue)':'var(--yellow)');
  h+=ms('Flagged',sh.flagged_count+' / '+s.sessions,sh.flagged_count<=2?'var(--green)':sh.flagged_count<=5?'var(--blue)':'var(--yellow)');
  h+=ms('Interrupts',String(sh.total_interrupts),sh.total_interrupts<=5?'var(--green)':sh.total_interrupts<=15?'var(--blue)':'var(--yellow)');
  h+=ms('Redundant reads',String(sh.redundant_reads||0),(sh.redundant_reads||0)<=2?'var(--green)':(sh.redundant_reads||0)<=10?'var(--blue)':'var(--yellow)');
  h+=ms('Plan mode',String(sh.plan_mode_uses||0),(sh.plan_mode_uses||0)>0?'var(--green)':'var(--dim)');
  h+=ms('Skills run',String(sh.skill_invocations||0),(sh.skill_invocations||0)>0?'var(--blue)':'var(--dim)');
  if(sh.subagent_spawns>0)h+=ms('Subagents',String(sh.subagent_spawns),'var(--blue)');
  if(sh.truncated_turns>0)h+=ms('Truncated',String(sh.truncated_turns),sh.truncated_turns<=2?'var(--green)':sh.truncated_turns<=10?'var(--blue)':'var(--orange)');
  h+='</div>';
  if(sh.top_flags&&sh.top_flags.length){
    h+='<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)">Most common patterns: ';
    h+=sh.top_flags.map(([n,c])=>`<span class="cat-pill" style="background:var(--yellow-dim,rgba(234,179,8,0.15));color:var(--yellow);margin-right:4px">${escHtml(FLAG_LABELS[n]||n)} ×${c}</span>`).join('');
    h+='</div>';
  }
  if(sh.top_skills&&sh.top_skills.length){
    h+='<div style="margin-top:8px;font-size:11px;color:var(--muted)">Top skills: ';
    h+=sh.top_skills.map(([n,c])=>`<span class="cat-pill" style="background:var(--blue-dim);color:var(--blue);margin-right:4px">${escHtml(n)} ×${c}</span>`).join('');
    h+='</div>';
  }
  return h+'</div>';
}
function ms(label,val,color){return`<div class="miniStat"><div class="v" style="color:${color}">${val}</div><div class="l">${label}</div></div>`}

// ── Trend ──
function renderTrend(t){
  if(!t)return'';
  let h='<div class="panel" style="margin-bottom:20px"><h3>Compared to prior period</h3><p class="panel-sub">Same-length window before this one. <span style="color:var(--green)">Green</span> = the change is in your favor (lower cost per session, higher cache rate, more sessions). <span style="color:var(--orange)">Orange</span> = the opposite.</p>';
  h+=tR('Tokens/msg',t.prev_tokens_per_msg,t.curr_tokens_per_msg,true,v=>v.toFixed(0));
  h+=tR('Cache rate',t.prev_cache_rate,t.curr_cache_rate,false,v=>pct(v));
  h+=tR('Cost/session',t.prev_cost_per_session,t.curr_cost_per_session,true,v=>fC(v));
  h+=tR('Total cost',t.prev_total_cost,t.curr_total_cost,true,v=>fC(v));
  h+=tR('Sessions',t.prev_sessions,t.curr_sessions,false,v=>v.toString());
  return h+'</div>';
}
function tR(l,p,c,lib,fn){if(p===0)return`<div class="tr"><div class="tm">${l}</div><div class="tv">${fn(c)}</div><span class="ta ta-s">new</span></div>`;const pc=((c-p)/p)*100;let cls,txt;if(Math.abs(pc)<5){cls='ta-s';txt=`~${Math.abs(pc).toFixed(0)}%`}else if(pc>0){cls=lib?'ta-ub':'ta-ug';txt=`+${pc.toFixed(0)}%`}else{cls=lib?'ta-dg':'ta-db';txt=`${pc.toFixed(0)}%`}return`<div class="tr"><div class="tm">${l}</div><div class="tv">${fn(p)} &rarr; ${fn(c)}</div><span class="ta ${cls}">${txt}</span></div>`}

// ── Score (small footer block) ──
function renderScore(sc_,e){
  return `<div style="display:flex;justify-content:center;margin-bottom:8px">
    <div class="score-tile" style="border-color:${sc(sc_.composite)};max-width:340px;width:100%">
      <div class="num" style="color:${sc(sc_.composite)}">${sc_.composite}</div>
      <div class="lbl">Overall</div>
      <div class="desc">Usage ${sc_.usage} (${escHtml(sc_.usage_band.toLowerCase())}) · Efficiency ${sc_.efficiency} (${escHtml(sc_.efficiency_band.toLowerCase())})</div>
    </div>
  </div>
  <p style="font-size:10px;color:var(--dim);text-align:center;margin-bottom:20px">Composite of usage and efficiency, weighted to penalize being very high in one and very low in the other.</p>`;
}

// ── Projects (only shown when >1; single table with inline cost bar) ──
function renderProjects(proj){
  if(!proj||proj.length<2)return'';
  const mxC=proj[0].cost;
  let h='<div class="panel" style="margin-bottom:20px"><h3>By project</h3><p class="panel-sub">Where the spend went across working directories</p>';
  h+='<table class="tt"><thead><tr><th>Project</th><th>Cost</th><th style="text-align:right">Sessions</th><th style="text-align:right">Messages</th><th style="text-align:right">Reads</th><th style="text-align:right">Writes</th><th style="text-align:right">External</th><th style="text-align:right">Eff.</th></tr></thead><tbody>';
  for(const p of proj){
    const ec=sc(p.efficiency);
    const w=mxC>0?Math.max((p.cost/mxC)*100,3):0;
    h+=`<tr><td title="${escHtml(p.name)}" style="font-weight:500">${escHtml(p.name)}</td>`;
    h+=`<td style="min-width:200px"><div style="display:flex;align-items:center;gap:8px"><div style="flex:1;height:14px;background:var(--s3);border-radius:3px;overflow:hidden"><div style="height:100%;width:${w}%;background:var(--accent);border-radius:3px"></div></div><div style="font-weight:600;width:55px;text-align:right">${fC(p.cost)}</div><div style="font-size:10px;color:var(--dim);width:36px;text-align:right">${pctR(p.cost_pct)}</div></div></td>`;
    h+=`<td style="text-align:right">${p.sessions}</td><td style="text-align:right">${p.messages}</td><td style="text-align:right">${p.reads}</td><td style="text-align:right">${p.writes}</td><td style="text-align:right">${p.mcp_calls}</td><td style="text-align:right;color:${ec};font-weight:600">${p.efficiency}</td></tr>`;
  }
  return h+'</tbody></table></div>';
}

// ── Empty state ──
function renderEmptyState(d){
  return `<div class="empty-state">
    <h3>Just a handful of sessions so far</h3>
    <p>Only ${d.summary.sessions} session(s) in the selected window. Insights get sharper once you've used Claude Code for a few weeks. The summary below still has data — it just won't reveal patterns yet.</p>
    <p style="font-size:11px">Total spend: <strong>${fC(d.summary.total_cost)}</strong> · Active days: <strong>${d.period.active_days}</strong> · Cache hit rate: <strong>${pctR(d.efficiency.cache_hit_rate)}</strong></p>
  </div>`;
}

// ── Render orchestration ──
function render(d){
  LAST_DATA=d;
  let h='';
  h+=renderHero(d);
  h+=renderColorKey();
  if(d.is_low_data){h+=renderEmptyState(d)}
  h+='<div class="section-h">Goals & opportunities</div>';
  h+=renderTargets(d.targets);
  h+=renderRecs(d.recs||{});
  h+='<div class="section-h">Where the money went</div>';
  h+=renderMoney(d);
  h+=renderModels(d);
  if(d.best_moments&&d.best_moments.length){h+='<div class="section-h">Highlights</div>';h+=renderBest(d.best_moments)}
  h+='<div class="section-h">How Claude Code got used</div>';
  h+='<div class="cols">'+renderActivity(d)+renderCategories(d.categories)+'</div>';
  h+=renderBloat(d.bloat_curves||[]);
  h+=renderDaily(d.daily||[]);
  h+=renderProjects(d.projects||[]);
  h+='<div class="section-h">Workflow quality</div>';
  h+=renderHealth(d.summary,d.session_health||{});
  h+='<div class="section-h">Score detail</div>';
  h+=renderScore(d.scores,d.efficiency);
  $('#dash').innerHTML=h;
}

// ── Session detail modal ──
async function openSession(sid){
  if(!sid)return;
  const mb=$('#modalBg'),m=$('#modal');
  mb.classList.add('open');
  m.innerHTML='<button class="modal-close" onclick="closeModal()">×</button><p>Loading session…</p>';
  try{
    const r=await fetch(`/api/session/${encodeURIComponent(sid)}`);
    const d=await r.json();
    if(d.error){m.innerHTML=`<button class="modal-close" onclick="closeModal()">×</button><p>${escHtml(d.error)}</p>`;return}
    renderModal(d);
  }catch(e){m.innerHTML=`<button class="modal-close" onclick="closeModal()">×</button><p>Error: ${escHtml(e.message)}</p>`}
}
function closeModal(){$('#modalBg').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});

function renderModal(d){
  const m=$('#modal');
  const cb=d.cost_breakdown||{};
  const cw=(cb.cache_write_5m||0)+(cb.cache_write_1h||0);
  let h='<button class="modal-close" onclick="closeModal()">×</button>';
  h+=`<h2>${escHtml(d.label)}</h2>`;
  h+=`<div class="modal-sub">${escHtml(d.project)} · <span class="cat-pill ${catClass(d.category)}">${escHtml(d.category)}</span> · ${d.messages_user+d.messages_assistant} messages · ${Math.round(d.duration_min)} min · ${fC(d.cost)} total</div>`;
  if(d.first_prompt){
    h+=`<div style="background:var(--s2);border-radius:6px;padding:10px 12px;margin-bottom:14px;font-size:12px;color:var(--muted);max-height:120px;overflow-y:auto"><strong style="color:var(--text)">First prompt:</strong> ${escHtml(d.first_prompt.slice(0,500))}${d.first_prompt.length>500?'…':''}</div>`;
  }
  if(d.flags&&d.flags.length){
    h+=`<div style="margin-bottom:14px;font-size:11px"><strong>Flags:</strong> ${d.flags.map(f=>`<span class="cat-pill" style="background:var(--orange-dim);color:var(--orange);margin-right:4px">${escHtml(f)}</span>`).join('')}</div>`;
  }
  h+=`<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px">`;
  h+=ms('Cache reads',fC(cb.cache_read||0),'#3b82f6');
  h+=ms('Cache writes',fC(cw),'#f97316');
  h+=ms('Output',fC(cb.output||0),'#16a34a');
  h+=ms('Input',fC(cb.input||0),'#6366f1');
  h+=`</div>`;
  if(d.tools&&d.tools.length){
    h+='<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Top tools</div>';
    for(const t of d.tools.slice(0,8)){h+=`<span class="cat-pill" style="margin-right:4px">${escHtml(t.friendly)} ×${t.count}</span>`}
    h+='</div>';
  }
  if(d.turns&&d.turns.length){
    h+='<div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Turn-by-turn</div>';
    h+='<div style="max-height:300px;overflow-y:auto"><table class="tt"><thead><tr><th>#</th><th style="text-align:right">Cost</th><th>Driver</th><th>Tool</th><th>Model</th><th style="text-align:right">Output</th><th style="text-align:right">Cache rd</th></tr></thead><tbody>';
    for(const t of d.turns){
      h+=`<tr><td>${t.idx}</td><td style="text-align:right;font-weight:600">${fC(t.cost)}</td><td style="font-size:10px;color:var(--muted)">${t.driver}</td><td>${escHtml(t.tool_friendly)}</td><td>${escHtml(t.model_friendly)}</td><td style="text-align:right">${fmt(t.output)}</td><td style="text-align:right">${fmt(t.cache_read)}</td></tr>`;
    }
    h+='</tbody></table></div>';
  }
  m.innerHTML=h;
}

init();
</script>
</body>
</html>"""


# ── Server ───────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    all_sessions = []

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/availability":
            self._json(scan_availability(Handler.all_sessions))
            return

        if parsed.path == "/api/analyze":
            # Accept ?from=YYYY-MM-DD&to=YYYY-MM-DD (preferred, what the dashboard uses)
            # OR ?days=N (convenience for curl/scripts — uses last N days from latest log).
            if "from" in qs and "to" in qs:
                df = datetime.strptime(qs["from"][0], "%Y-%m-%d").date()
                dt = datetime.strptime(qs["to"][0], "%Y-%m-%d").date()
            else:
                days = int(qs.get("days", ["30"])[0])
                avail = scan_availability(Handler.all_sessions)
                if avail["last_date"]:
                    dt = datetime.fromisoformat(avail["last_date"]).date()
                    df = dt - timedelta(days=days - 1)
                else:
                    df = dt = datetime.fromisoformat(_today()).date()
            self._json(analyze(Handler.all_sessions, df, dt))
            return

        if parsed.path.startswith("/api/session/"):
            sid = parsed.path.rsplit("/", 1)[-1]
            self._json(session_detail(Handler.all_sessions, sid))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=_json_default).encode())

    def log_message(self, *a):
        pass


def _json_default(o):
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Not JSON serializable: {type(o)}")


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def main():
    parser = argparse.ArgumentParser(description="Claude Code Efficiency Analyzer — how you actually use Claude Code")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--export", metavar="OUT.json", help="Write report JSON for the period and exit")
    parser.add_argument("--days", type=int, default=30, help="Days for --export (default 30)")
    parser.add_argument("--demo", nargs='?', const='solo-dev', default=None,
                        metavar="PERSONA",
                        help="Use synthetic demo data instead of your local logs. "
                             "Persona: solo-dev (default), pm, writer.")
    args = parser.parse_args()

    if args.demo is not None:
        if args.demo not in DEMO_PERSONAS:
            print(f"Unknown demo persona '{args.demo}'. Choose: {', '.join(DEMO_PERSONAS)}")
            return
        print(f"Generating demo data (persona='{args.demo}', synthetic, no real sessions)...")
        all_sessions = generate_demo_sessions(persona=args.demo)
        print(f"Generated {len(all_sessions)} demo sessions.\n")
    else:
        print("Loading session logs...")
        all_sessions = load_all_sessions()
        print(f"Loaded {len(all_sessions)} sessions.\n")

    if args.export:
        avail = scan_availability(all_sessions)
        if not avail["last_date"]:
            print("No sessions found.")
            return
        last = datetime.fromisoformat(avail["last_date"]).date()
        first = last - timedelta(days=args.days - 1)
        report = analyze(all_sessions, first, last)
        with open(args.export, "w") as f:
            json.dump(report, f, default=_json_default, indent=2)
        print(f"Wrote {args.export} ({first} → {last})")
        return

    Handler.all_sessions = all_sessions
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
