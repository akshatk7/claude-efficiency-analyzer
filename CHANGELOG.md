# Changelog

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
