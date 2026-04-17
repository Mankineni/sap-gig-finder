# SAP Gig Finder

## Links

- **GitHub**: https://github.com/Mankineni/sap-gig-finder
- **Local path**: `C:\Users\PavanMankineni\Agents\sap-gig-finder`
- **Live dashboard**: https://mankineni.github.io/sap-gig-finder/

## What this project does

Multi-agent pipeline that scrapes SAP freelance gigs from GULP, Freelancermap, and EURSAP, scores them with Claude, validates URLs, and publishes a PWA dashboard to GitHub Pages. Runs daily at 06:00 CET via GitHub Actions.

## Architecture

Four agents connected by two `asyncio.Queue`s:

```
Scout (Agent A) → raw_queue → Analyst (Agent B) → analyzed_queue → Validator (Agent D) → Formatter (Agent C)
```

- **Scout** (`agents/scout.py`): Playwright scrapers for 3 sources (GULP, Freelancermap, EURSAP) + stubs for LinkedIn/Upwork. Pushes `RawListing` objects. Uses `SENTINEL` to signal completion.
- **Analyst** (`agents/analyst.py`): Calls Claude API to score listings 1-10. Filters out `score < MIN_SCORE` (6). Pushes `AnalyzedListing` objects.
- **Validator** (`agents/validator.py`): HTTP HEAD checks, Playwright expiry detection for GULP/EURSAP, URL deduplication. Produces `ValidatedListing` objects.
- **Formatter** (`agents/formatter.py`): Generates Markdown report + JSON output, copies to `docs/gigs_latest.json` for the dashboard.

## Key files

| File | Purpose |
|---|---|
| `agents/models.py` | `RawListing` → `AnalyzedListing` → `ValidatedListing` dataclasses |
| `agents/scout.py` | Playwright scrapers, `SENTINEL` export |
| `agents/analyst.py` | Claude API scoring, `ANALYST_SYSTEM_PROMPT` |
| `agents/validator.py` | HTTP checks, dedup, expiry detection |
| `agents/formatter.py` | Markdown/JSON report generation |
| `config/settings.py` | All constants, API keys, source URLs, search queries |
| `scheduler/run_pipeline.py` | Pipeline orchestrator, CLI entry point |
| `docs/index.html` | PWA dashboard (vanilla JS, no dependencies) |
| `docs/sw.js` | Service worker (network-first for data, cache-first for shell) |
| `.github/workflows/weekly_scan.yml` | Daily scan + Pages deploy |
| `.github/workflows/pages.yml` | Standalone Pages deploy on docs/ changes |

## Commands

```bash
# Run the full pipeline
python -m scheduler.run_pipeline

# Dry run (scrape + score, skip validation and file output)
python -m scheduler.run_pipeline --dry-run

# Run tests
python -m pytest tests/ -v

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

## Configuration

All settings in `config/settings.py`. Key constants:

- `ANTHROPIC_MODEL`: claude-sonnet-4-20250514
- `MIN_SCORE`: 6 (listings below this are filtered out)
- `MAX_ANALYST_CONCURRENCY`: 4
- `MAX_VALIDATOR_CONCURRENCY`: 3
- `PIPELINE_TIMEOUT_SECONDS`: 600
- `SEARCH_QUERIES`: 3 SAP-focused search strings

API keys loaded from `.env` (gitignored). See `.env.example` for required keys.

## Sentinel pattern

Workers use sentinel objects to signal queue completion:
1. `SCOUT_SENTINEL` → pushed by scouts into `raw_queue` after all scraping finishes
2. `ANALYST_SENTINEL` → pushed by analysts into `analyzed_queue` after all scoring finishes
3. Workers re-queue the sentinel so sibling workers blocked on `queue.get()` also wake up

## Tests

19 tests across 4 files — all must pass before pushing:
- `tests/test_models.py` — dataclass instantiation and inheritance
- `tests/test_analyst.py` — Claude API mocking, JSON parsing, score filtering
- `tests/test_validator.py` — HTTP status mapping, dedup, expiry detection
- `tests/test_pipeline.py` — sentinel propagation, queue backpressure

## Deployment

- GitHub Pages source: GitHub Actions
- Scan schedule: daily at 04:00 UTC (06:00 CET)
- The scan workflow deploys Pages directly (GitHub doesn't trigger workflows from bot commits)
- See `DEPLOY.md` for full setup checklist
