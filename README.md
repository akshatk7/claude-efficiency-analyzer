# Claude Code Efficiency Analyzer

A single-file Python tool that reads your local Claude Code session logs and shows you, in a browser dashboard, where your spend actually went, how you've been using Claude, and one concrete thing you could change. Local-only. Stdlib only. No API keys.

![Screenshot](screenshot.png)

## Run it

```bash
git clone https://github.com/akshatk7/claude-efficiency-analyzer.git
cd claude-efficiency-analyzer
python3 analyzer.py
```

Opens your browser at `http://localhost:8741`. `Ctrl+C` to stop. Python 3.8+, no dependencies.

If you don't have Claude Code logs yet:

```bash
python3 analyzer.py --demo            # solo-dev preview
python3 analyzer.py --demo pm         # PM persona (Snowflake/Slack/Granola/Figma)
python3 analyzer.py --demo writer     # writer/researcher persona
```

`python3 analyzer.py --help` for full flags (custom port, JSON export, date range, etc.).

## What you'll see

- **Where the money went** — the load-bearing insight. For nearly all real users, 80%+ of spend is cache reads (Claude re-reading the conversation each turn), not output. The dashboard explains why and names the lever.
- **One concrete action** — a single high-leverage recommendation with quantified savings, e.g. *"$60 of your spend was Opus on trivial reads. Switch those to Sonnet."*
- **Goals** — progress bars vs. targets for cache hit rate, average session depth, and Opus-on-trivial-work share.
- **Best moments** — your longest clean session and most productive (writes-per-dollar) session.
- **What you used Claude for** — sessions auto-categorized by intent (Coding, Writing, Research, Data Analysis, etc.).
- **Context bloat** — per-turn cache-read sparklines for your three heaviest sessions. A rising staircase means stale context that should be `/clear`'d.
- **Daily spend** and **session health signals** for the diagnostic view.

## The cache aha

Every Claude Code turn re-reads the entire conversation history: every prior message, file read, and tool result. With long sessions, that compounds fast. Most people associate "AI cost" with output tokens, but output is usually 10–15% of the bill. Cache reads dominate.

The cache itself isn't the problem (a 96% hit rate just means context is being reused efficiently). The lever is **session length**: use `/clear` between unrelated tasks, or start a fresh session, instead of letting one session run for hours.

## Privacy

- Reads `~/.claude/projects/`. That's it.
- Local HTTP server bound to `127.0.0.1`. Not reachable from your network.
- No outbound calls, no telemetry, no analytics.
- Read-only on your logs.
- Stdlib only. Verify in `analyzer.py` (the only network code is `http.server` and `webbrowser.open()`).

## A note on plans

Cost numbers use [Anthropic's API pricing](https://platform.claude.com/docs/en/about-claude/pricing). If you're on Pro or Max (flat monthly), the dollar amounts shown are what your usage *would* cost at API rates, not your actual bill. The efficiency insights are still useful: better efficiency means hitting your plan's limits less often.

Claude Code keeps the last 30 days of logs locally by default. To extend, set `cleanupPeriodDays` in `~/.claude/settings.json` (longer retention = more disk).

## License

MIT
