# Changelog

## v1.4.3 — 2026-05-01

Two small but meaningful fixes prompted by user inspection.

### Retention transparency
- Dashboard now reads `cleanupPeriodDays` from `~/.claude/settings.json` and surfaces it in the availability bar: "Claude Code keeps the last **30 days** locally by default — older sessions auto-rotate. Bump `cleanupPeriodDays` if you want a longer history." (or shows your custom value if set)
- README "Data Availability" section rewritten to explain the 30-day default explicitly, with a config example for users who want longer history

### "Active hours" metric fixed
- Old behavior: `duration_min = max(timestamps) - min(timestamps)` per session, summed across sessions. A session left open in a tab for a week registered as 168 hours of "active" time. With many parallel sessions, totals could exceed 24h/day
- New behavior: `duration_min` is the sum of gaps between consecutive timestamps within a session, capping each gap at 5 minutes. Filters out idle stretches and parallel-tab inflation
- Real-data example: 768h → 40h for a 30-day window. Reflects actual focused use
- New `span_min` field retains the wall-clock span if needed elsewhere

## v1.4.2 — 2026-05-01

UX polish round addressing the critique items from v1.4.1.

### Hero
- `compute_hero_headline()` returns an actual insight instead of restating stats. Picks from candidates: cache-vs-output ratio, cache % of total, top-5 sessions concentration, hours-as-percent-of-work-week, cost-per-active-day, MCP-heavy framing. Whichever scores highest wins
- "22/25" active days → "22 of 25 days"

### Recs
- "Where to focus" caps at top 3 primary + 3 secondary, sorted by `savings` descending. Overflow goes under a `+ N more` `<details>` expander
- No more 5-7 item recommendation walls

### Best Moments
- Reframed: leads with duration ("6h 22m") + messages + writes. Cost is now a footer line, not the headline
- Duration formatted as `Xh Ym` for sessions over an hour

### Daily Spend
- Color legend at the top (`under $30` green, `$30-100` blue, `over $100` orange) so the bar colors aren't a guessing game

### By Project
- Single table view with an inline cost-bar in the Cost column. Removed the redundant standalone bar chart

### Trend
- Explicit semantics in the panel sub: "Green = the change is in your favor (lower cost per session, higher cache rate, more sessions). Orange = the opposite."

### Score Detail
- Three near-identical tiles → one composite tile with a sub-line: `Usage 92 (heavy) · Efficiency 91 (high)`
- Formula description condensed to one line

### Cache Explainer
- The "Why is X% of your spend in cache?" block is now collapsed in a `<details>` element with a click-to-expand summary. Visible but doesn't dominate on every load for repeat users

### Category Drill-Down
- Click any category in "What you used Claude Code for" → filters Top 10 most expensive turns to that category, smooth-scrolls to the table, shows a `Filtered to X · clear` banner
- Click again or hit "clear" to remove the filter
- Filter resets when changing dates

### Friendly Names
- Top Turns "Driver" column already showed friendly labels (`Cache write`, `Output`, etc.) — verified consistent

## v1.4.1 — 2026-05-01

Demo mode + fresh screenshot.

- New `--demo` flag generates 85 synthetic sessions across 32 days with realistic distributions (Coding-heavy, then Data Analysis / PM Work, mix of long and short sessions, power-law-ish cost spread, sprinkled flags). Deterministic via fixed seed. No real data
- `screenshot.png` regenerated from `--demo` output — shows the v1.4 dashboard end-to-end
- README adds a "No logs yet? Preview the dashboard" subsection right after Quick Start

## v1.4.0 — 2026-05-01

Major UX overhaul. The dashboard is now built around *what to do*, not *what your score is*. Adds drill-down, privacy mode, print export, session labels, category inference, quantified savings, and a Pro/Max plan banner. Score is demoted from hero to footer.

### New: TL;DR Hero
- Top of the dashboard is a hero block with a one-line headline (period spend, sessions, hours), a single highest-impact action with quantified $ savings, four key numbers, and a profile band
- Replaces the "Score 100 + 83 = 90" arithmetic that read like math but wasn't

### New: Goals & Targets
- Progress bars vs explicit targets for cache hit rate, active days/week, average session depth, and Opus-on-trivial-work %
- Vertical line marks the target — checkmark on metrics already on track
- Replaces "what's a good number?" with "here's the target, here's where you are"

### New: Quantified Recommendations
- Recs are split into "Where to focus" (top opportunities) and "Going well"
- Top opportunities lead with estimated $ savings: "Stale context piling up <span>~$60</span>"
- Five concrete savings models computed: stale context, Opus-on-trivial, MCP-heavy, short sessions, vague prompts
- The biggest opportunity becomes the hero action

### New: Session Labels & Drill-Down
- Every session has a human-readable label derived from its first user prompt (first 80 chars)
- Click any session label or Top-10 row → modal with project, category, cost split, top tools, first prompt, flags, and full turn-by-turn cost table
- New `/api/session/<id>` endpoint backs the modal

### New: Session Categorization
- Sessions classified by inferred intent: Coding / Data Analysis / PM Work / Design Work / Debugging / Research / Writing / Other
- Heuristic uses tool mix (Snowflake → Data Analysis, Granola/Slack → PM Work, Figma → Design) plus prompt keywords
- "What you used Claude Code for" panel shows cost share by category

### New: Best Moments
- Surfaces the longest clean session and the highest-output-per-dollar session
- Counterbalances the diagnostic tone with positive reinforcement

### New: Top Cost Drivers
- "Top 10 Most Expensive Turns" now shows the dominant cost driver per turn (cache_read / cache_write / output / input / web_search)
- Tool names use friendly labels everywhere (Browser screenshots, Snowflake queries, Slack threads, etc.)

### New: Privacy Mode
- Header toggle redacts /Users/ paths and session IDs to stable aliases (project-A, session-h4sh)
- Persists to localStorage; pass `--privacy` to default it on
- Note: free-text content (first prompts) isn't auto-scrubbed — those can contain anything

### New: Print / Export PDF
- "Print / PDF" button in the header
- Print stylesheet hides controls, modals, footer; pages break inside panels cleanly

### New: Pro/Max Plan Banner
- First-load banner explaining the dollar amounts are API-equivalent, not your actual bill
- Dismissible (localStorage)
- Addresses the most common confusion for plan users

### Re-toned & Reordered
- Tier labels: "Power Use / Heavy Use / Steady Use / Light Use / Just Starting" (descriptive, not graded)
- Dropped "Under-utilizing" / "Needs Attention" / "Getting Started" — too judgmental for an OSS tool
- New panel order: TL;DR → Goals → Recommendations → Money → Models + External → Best Moments → Activity + Categories → Top Turns → Bloat → Daily → Projects → Health → Trend → Score
- Score panel moved to footer with explicit formula
- Color key at top explains green/orange/yellow/blue semantics
- Section headers ("Goals & opportunities", "Where the money went", etc.) group related panels
- Single-project users no longer see a wasted "By Project" table — the panel only renders with 2+ projects

### Insight Quality
- Recommendations deduped — "External Service Costs" no longer appears in three places
- Friendly tool names applied everywhere (Top 10, Activity, External, Health top-skills, drill-down)
- Empty-state UI for users with fewer than 5 sessions
- Forecast extended to yearly: "$X/mo · $Y/yr"

### Architecture
- New `friendly_tool_name`, `friendly_model_name`, `categorize_session`, `extract_session_label` helpers
- `dominant_cost_driver()` per-turn tagging
- `compute_top_finding()` ranks savings opportunities
- `compute_best_moments()` surfaces positive sessions
- `compute_targets()` produces progress-bar data
- `redact_obj()` applies path/ID redaction with stable aliases
- HTML render split into ~15 per-section JS helpers, each ~10-30 lines

## v1.3.0 — 2026-05-01

Major refresh for the Opus 4.7 / 1M-context era. Cost numbers are now correct end-to-end, the score reflects modern Opus-heavy workflows, and several previously-invisible signals are now surfaced.

### Pricing & Cost Correctness
- **Opus 4.7 added** to pricing table at $5/$25 per MTok (released 2026-04-16; same rate as 4.6)
- **5-minute vs 1-hour cache writes priced separately.** 5m at 1.25× input, 1h at 2× input. Logs now expose `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` and the analyzer respects the split
- **Per-token-type cost is now per-message-aggregated** (not "dominant model × all tokens"). Mixed-model periods now show correct breakdowns
- **`web_search` server-tool calls** billed at $10/1,000 and surfaced as a separate slice in "Where Your Money Goes"
- **Better MCP token estimate.** Uses cache-creation delta from the next assistant turn weighted by tool-result size, with a char-based fallback. More accurate for JSON-dense services (Snowflake, Slack, Granola)
- **Repeated-read false-positive fixed.** `(file_path, offset, limit)` is the dedup key — paginated reads of the same file no longer flag

### Score Rebalanced for the Opus / 1M-Context Era
- **Dropped the flat "Opus % penalty"** that pulled scores down for following the "always use the latest Opus" workflow
- **New task-fit signal (15%)**: penalizes Opus only when spent on trivial reads/Bash-only turns where Sonnet would suffice
- **Cache hit rate thresholds raised**: top tier now requires >90% (was >85%)
- **Stale-context flag**: sessions with avg cache-reads above 300K/turn are flagged with a `/clear` recommendation

### New Panels
- **Top 10 Most Expensive Turns** — single-message cost leaderboard with model + tool
- **Context Bloat — Top 3 Sessions** — per-turn cache-read sparklines showing the rising-staircase failure mode
- **Skills, Plan Mode, Subagents** mini-stats in Session Health, plus a top-skills chip strip

### New Signals
- `stop_reason: max_tokens` — counts truncated responses; flagged if >5% of assistant turns
- `ExitPlanMode` invocations + `permissionMode: plan` records — rewarded in efficiency recs
- `Skill` tool invocations — leaderboard of which skills you use most
- `Task` / Agent spawns — surfaced in Session Health
- 1h vs 5m cache-write split visible in summary

### CLI
- `--export OUT.json` flag writes a full analysis report and exits — use it to diff month-over-month or pipe into your own tooling
- `--days N` controls the export window (default 30)

### Architecture
- **Single-pass JSONL loading** — each session file is now parsed once at startup, then sliced in-memory for any date window. Cuts analyze-call latency to ~0
- **`parse_session` split** into focused helpers (`_parse_user_message`, `_parse_assistant_message`, `_parse_record`)
- **Project-name resolution memoized** — no more re-reading the first 3 jsonls of every project on every analyze call
- **`cost_components` returns a typed dict** so per-component aggregation is straightforward
- **HTML render code split** into per-panel `render_*` JS helpers — far easier to extend

### Notes
- The screenshot in this repo is the v1.2.0 layout; the v1.3 panels (top-10 turns, bloat curves) appear below the existing panels and use the same visual language. Will refresh with anonymized data in a future patch

## v1.2.0 — 2026-03-31

### Per-Project Breakdown
- Cost, sessions, messages, reads, writes, external calls, and efficiency per project
- Cost bar chart comparing projects side-by-side (when multiple projects exist)
- Human-readable project names derived from working directory (e.g. `Projects/my-app`)
- Tip for users launching from a single directory to get per-project tracking

### Housekeeping
- Anonymized screenshot with mock data (no real usage data in repo)

## v1.1.0 — 2026-03-30

Major redesign for accessibility and deeper analysis.

### UX
- Light theme replacing dark theme
- Plain-English insights replacing raw gauge walls
- "What You're Doing Well" + "Where You Can Improve" sections with actionable recommendations
- Friendly names for models ("Opus" not "claude-opus-4-6") and services ("Screenshots" not "playwright::browser_take_screenshot")
- Activity breakdown as category summary bar (Reading & Searching / Writing & Editing / External Services)
- Daily chart capped to 14 most recent days

### Cost Analysis
- "Where Your Money Goes" stacked bar showing cost by token type (cache reads, cache writes, output, input)
- Cache cost explainer: why 90%+ of cost is cache, what the 1M context window means, and what to do about it
- Estimated dollar cost next to each external service
- Corrected pricing: Opus 4.6 at $5/$25 (was incorrectly using old $15/$75 rates)

### Efficiency Analysis
- 6-signal efficiency score: cache hit rate, session depth, response leanness, model mix, read:write ratio, session health
- Session health flags: bulk-read-no-output, MCP-heavy, low-interaction, high-interrupts, deep-agent-loops, redundant-reads
- Prompt specificity tracking (short vague prompts vs. detailed ones)
- Agent loop detection (consecutive Claude turns without user input)
- Redundant file read detection (same file read 2+ times in a session)
- MCP token weight analysis (not just call count — estimated tokens per result)
- Consolidated recommendations (no duplicate cards)

### Data
- Per-message daily cost attribution (not per-session)
- Data availability scan with date range bounding
- Trend comparison vs. prior equivalent period

## v1.0.0 — 2026-03-29

Initial release.
