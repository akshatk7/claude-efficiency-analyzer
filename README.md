# Claude Code Efficiency Analyzer

A single-file tool that reads your local Claude Code session logs and helps you understand your spending, token distribution, and where you can be more efficient. No API keys, no dependencies, no data leaves your machine.

![Screenshot](screenshot.png)

## Quick Start

```bash
git clone https://github.com/akshatk7/claude-efficiency-analyzer.git
cd claude-efficiency-analyzer
python3 analyzer.py
```

That's it. Opens your browser with the report. Just Python 3.8+ (stdlib only).

To stop: `Ctrl+C` in the terminal.

## What You Get

**Score** — A composite score combining Usage (how much you use Claude Code) and Efficiency (how well you use it). You need both to score high.

**Where Your Money Goes** — Cost breakdown by token type. Most people are surprised to learn that 90%+ of their cost is cache reads and writes (Claude re-reading your conversation history every turn), not the actual responses. The tool explains why and what you can do about it.

**What You're Doing Well / Where You Can Improve** — Plain-English insights based on your actual usage patterns: session depth, cache reuse, model mix, prompt specificity, agent loops, redundant file reads, interrupt rate, and external service costs.

**Activity Breakdown** — What Claude spends its time doing: reading & searching vs. writing & editing vs. external services (Slack, Snowflake, Google Docs, Figma, etc.).

**Cost by Model & Service** — Which models and external integrations cost the most, with estimated dollar amounts.

**Daily Spend** — How much you spent each day across the selected period.

**By Project** — Per-project cost, sessions, and efficiency breakdown. See which projects consume the most tokens and where each one's efficiency stands. Launch Claude Code from specific directories (`cd ~/Projects/my-app && claude`) to get automatic per-project tracking.

**Session Health** — Waste pattern detection: bulk reads without output, heavy external service usage, frequent interrupts, redundant file reads.

## Key Insight: Why Cache Costs Dominate

Every time you send a message, Claude re-reads your **entire conversation history**. With the 1M context window model, sessions can grow very long, and Claude reprocesses all of that context on every single turn.

Early in a session, this is cheap. But by message 100+, Claude may be re-reading 500K+ tokens per turn. That's where cost accumulates — not from Claude's responses, but from re-reading the conversation over and over.

**What you can do:** Start fresh sessions when switching tasks. Use `/clear` to reset context mid-session. The 1M window is powerful for deep work, but leaving stale context loaded means you're paying to re-read things Claude no longer needs.

## Data Availability

The tool can only analyze logs that exist on your machine. It tells you exactly what date range is available:

> Your logs cover **2026-02-15** to **2026-03-31** (45 active days, 124 session files)

Logs may not go back to when you first started using Claude Code — older sessions can be rotated out, and usage on other machines won't appear locally.

## Who Is This For?

**API / Enterprise users** get the most value — cost estimates directly reflect what you're paying per token, so you can optimize spending.

**Max plan users** ($100-200/mo) pay a flat monthly fee, not per-token. The dollar amounts shown are *what your usage would cost at API rates*, not what you're actually paying. But the efficiency insights are still valuable: better efficiency means you hit your usage limits less often and get more done per session.

**Pro plan users** ($20/mo) — same as Max. Cost numbers are illustrative, but efficiency patterns, session health, and workflow insights still help you work more effectively within your plan's limits.

## Pricing

Cost estimates use [Anthropic's published API pricing](https://platform.claude.com/docs/en/about-claude/pricing):

| Model | Input | Output | Cache Read | Cache Write |
|---|---|---|---|---|
| Opus 4.6 | $5/MTok | $25/MTok | $0.50/MTok | $6.25/MTok |
| Sonnet 4.6 | $3/MTok | $15/MTok | $0.30/MTok | $3.75/MTok |
| Haiku 4.5 | $1/MTok | $5/MTok | $0.10/MTok | $1.25/MTok |

These are API rates. Enterprise plans may have different negotiated rates. Pro and Max plans are flat-fee — cost numbers shown are equivalent API cost, not your actual bill.

## Options

```bash
python3 analyzer.py              # default port 8741
python3 analyzer.py --port 9000  # custom port
python3 analyzer.py --no-open    # don't auto-open browser
```

## Requirements

- Python 3.8+
- No external dependencies
- macOS or Linux (anywhere `~/.claude/projects/` exists)

## License

MIT
