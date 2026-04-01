# Contributing

Thanks for your interest in contributing to Claude Code Efficiency Analyzer!

## How to contribute

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Test locally: `python3 analyzer.py`
4. Open a pull request with a clear description of what you changed and why

## Design principles

- **Single file.** The entire tool is one Python file with zero external dependencies. This is intentional. Don't add dependencies.
- **No data leaves the machine.** The tool reads local logs and serves a local dashboard. No analytics, no telemetry, no network calls.
- **Stdlib only.** Python 3.8+ standard library. No pip installs.
- **Plain English over dashboards.** Prefer actionable insights in words over raw numbers and charts.

## What's helpful

- Bug fixes (especially around log parsing edge cases)
- New efficiency signals or recommendations
- Better explanations of existing insights
- Support for additional platforms (Windows, etc.)
- UI/UX improvements to the dashboard

## What to avoid

- Adding external dependencies
- Sending data anywhere
- Breaking the single-file design (the HTML stays embedded in `analyzer.py`)
- Adding features that require configuration files

## Questions?

Open an issue and we'll figure it out.
