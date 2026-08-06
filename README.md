# speedhive-tools

A Python client, SQLite persistence layer, analyzers, and CLI for scraping and analyzing [MyLaps Speedhive](https://speedhive.mylaps.com) racing results. It powers the [`speedhive-tools-ui`](https://github.com/ncrosty58/speedhive-tools-ui) dashboard (which vendors it as a git submodule), but works standalone with no web UI required.

## Install

```bash
pip install -e .            # library + `speedhive` CLI
pip install -e ".[dev]"     # + pytest for development
```

Requires Python 3.10+.

## What's in the package

- **`speedhive.wrapper.SpeedhiveClient`** — ergonomic HTTP client over an OpenAPI-generated core (`speedhive/generated/`): organizations, events, sessions, results, laps, lap charts, announcements, championships.
- **`speedhive.storage.SpeedhiveStorage`** — SQLite persistence for everything the client fetches, with per-entity save/read and org-scoped pruning.
- **`speedhive.workflows`** — org sync (`refresh_org_cache`, incremental or full with event caps and recent-event backfill), NDJSON dump import (`import_sqlite_dump`), and the track-record curation pipeline (`track_records/`).
- **`speedhive.analyzers`** — the statistics engines:
  - `analyze_consistency` — per-driver lap-time consistency (mean / stdev / CV with outlier filtering and fuzzy name clustering), plus most-improved/most-declined rankings.
  - `analyze_results` — race-results analysis: wins and podiums by class finishing position, and the all-drivers directory (starts/wins/podiums with precomputed ranks).
  - `analyze_class_pace` — average lap time per class by year, and participation-by-year breakdowns.
  - `analyze_driver_laps` — per-driver lap extraction.
- **`speedhive.settings`** — per-org settings resolution backed by `<SPEEDHIVE_DATA_DIR>/orgs/<org_id>/settings.json`: an org's own override wins, else the global environment default applies (per-org env vars use a `NAME_<org_id>` suffix). Shared by the CLI and host apps, so both always agree on parser engine, Gemini credentials, and min-laps. Email settings are deliberately not part of this — email is host-app policy (`speedhive-tools-ui` uses Resend); the library's only built-in notification path is an optional Gotify push in the curation workflow.
- **`speedhive.exporters`** — NDJSON exporters for every entity, lap records, curated records, and full portable dumps.
- **`speedhive.llm`** — optional Gemini-based announcer-text extraction, selected per org via settings.

## CLI

Everything is available as `speedhive <command>`:

```bash
# Sync an org into the local SQLite cache (incremental by default)
speedhive sync-org --org 30476
speedhive sync-org --org 30476 --mode full --max-events 150

# Raw scrape straight to NDJSON files, no database
speedhive export-dump --org 30476 --output ./dump

# Analysis reports from the cache
speedhive report-consistency --org 30476 --min-laps 20 --top 10 --ignore-outliers
speedhive extract-driver-laps --org 30476 --driver "Jane Doe"

# Track records: refresh + scan, or scan the existing cache only
speedhive refresh-track-records --org 30476 --mode incremental
speedhive scan-track-records --org 30476
speedhive export-track-records --org 30476 --output records.ndjson

# Curated-list portability
speedhive export-curated-track-records --org 30476 --output curated.ndjson
speedhive import-curated-track-records --org 30476 --input curated.ndjson --mode merge

# Cache portability
speedhive export-db-dump --org 30476 --output-dir ./backup
speedhive import-dump --org 30476 --dump-dir ./backup
speedhive export-lap-records --org 30476 -o lap-records.ndjson

# Interactive per-org configuration (parser engine, Gemini key/model, min-laps)
speedhive configure --org 30476
```

Subcommands are auto-discovered from the `exporters`/`workflows`/`analyzers` modules; a few module-derived names are aliased onto the canonical commands (`analyze-consistency` → `report-consistency`, `refresh-org-cache` → `sync-org`, `import-sqlite-dump` → `import-dump`, `export-full-dump` → `export-dump`). Run `speedhive <command> --help` for full flags.

## Python API

```python
from speedhive.wrapper import SpeedhiveClient
from speedhive.storage import SpeedhiveStorage

client = SpeedhiveClient.create()
storage = SpeedhiveStorage("speedhive.db")

org = client.get_organization(30476)
for event in client.iter_events(30476):
    print(event["name"])
```

```python
# Analysis from an already-synced cache
from speedhive.analyzers.analyze_results import get_wins_podiums_rankings
from speedhive.analyzers.analyze_consistency import load_session_types_from_storage

results = storage.load_results_payloads(30476)
session_map = load_session_types_from_storage(storage, 30476)
most_wins, most_podiums = get_wins_podiums_rankings(results, session_map)
```

### Rate limiting

`HTTPXRetryTransport` retries 429/502/503/504 *after* they happen. For bulk work
against the global feed that is not enough, so a client can also be given a
proactive token bucket and an in-flight ceiling. It is opt-in: a client without
one behaves exactly as before.

```python
from speedhive.client import Client
from speedhive.ratelimit import RateLimiter
from speedhive.wrapper import SpeedhiveClient

limiter = RateLimiter(requests_per_second=6.0, max_concurrency=4)
client = SpeedhiveClient(client=Client(base_url="https://api2.mylaps.com", rate_limiter=limiter))

print(limiter.stats())   # granted, waited_seconds, and the configured limits
```

The limiter paces every attempt, retries included, and one instance is safe to
share across threads. `RateLimiter` takes injectable `monotonic` and `sleep`
callables so pacing can be tested without spending real time.

## Configuration

| Variable | Description | Default |
| :-- | :-- | :-- |
| `SPEEDHIVE_DATA_DIR` | Root for per-org settings and data | `./data` |
| `SPEEDHIVE_DB_PATH` | SQLite database path | `<data dir>/speedhive.db` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Gemini credentials for AI-assisted parsing (per-org: `GEMINI_API_KEY_<org_id>`) | unset |
| `TRACK_RECORDS_STALE_HOURS` | Cache age before `refresh_and_scan` re-syncs | `20` |
| `GOTIFY_URL` / `GOTIFY_APP_TOKEN` | Optional Gotify push on new record candidates | unset |

Per-org file layout under `SPEEDHIVE_DATA_DIR`:

```
orgs/<org_id>/
├── settings.json             # per-org overrides (parser engine, Gemini, min-laps, alias map path)
└── track_records/
    ├── curated.ndjson        # approved records (each with its own edit history)
    ├── candidates_pending.ndjson
    ├── rejected.ndjson
    └── ...                   # parse cache
```

## Development

```bash
pip install -e ".[dev]"
pytest            # test suite
ruff check src/
```

`examples/` contains runnable scripts for common client and analyzer tasks.
