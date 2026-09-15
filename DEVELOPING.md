# Developing sardinetracker

For people reading or changing the code. If you want to contribute, read [CONTRIBUTING.md](CONTRIBUTING.md) first; this file is the map.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt    # the app's requirements plus pytest
python -m pytest
```

## How the app is put together

`app.py` is the entry point. Importing it assembles the whole app:

| Module | What it is |
|---|---|
| `appcore.py` | The Flask app itself: config, database migrations, secret key and CSRF, login, request hooks |
| `flaremodel.py` | The flare model's shared scoring layer: weights, multi-day context, cycle detection, the flare score. Pages, the API and reminders all use it, so it lives in none of them |
| `scoring.py` | Pure scoring primitives. No database, no Flask, no I/O, so it's safe to import from anywhere |
| `reminders.py` | ntfy notifications and scheduled jobs. Importing it starts the scheduler |
| `routes/` | The pages and API, one module per area of the app. Importing a module registers its URLs |

Two rules hold across all of them: **data access goes through `db.py`**, and **UV fetching goes through `uv_fetcher.py`**.

### Where data lives

The environment variable `SARDINE_DATA_DIR` names the folder for `biotracking.db`, `config.json` and `documents/`. The Android local app sets it (along with `SARDINE_EMBEDDED` and `SARDINE_NOTIFY_QUEUE`) to run this same code on the phone. Without it, `config.json` and `documents/` are found in the repo folder but the database is opened in the *current* directory, so run the app from the repo folder.

## Tests

`tests/conftest.py` makes the suite safe to run anywhere, including a fresh clone with no `config.json`:

- Before any app module is imported, the run moves into a throwaway folder, points `SARDINE_DATA_DIR` at it, writes a `config.json` from `config.json.example`, and builds an empty database. Nothing a test does can reach a real database.
- Outbound network connections are refused for the whole run.
- The background scheduler never starts.

Two fixtures do most of the work: `fresh_db`, an empty migrated database of the test's own, for tests that write; and `app_client`, a test client for the whole app with CSRF checks off.

| File | Covers |
|---|---|
| `test_scoring.py` | The pure scoring primitives |
| `test_flaremodel.py` | The scoring layer, on a throwaway data folder |
| `test_safety.py` | Rules that must keep holding: the clinician portal is read-only, every page needs a login except `PUBLIC_ENDPOINTS`, API tokens are checked properly, login only redirects within the site |
| `test_own_data_only.py` | Deleting and downloading an account touch only that account's data |
| `test_lab_import.py` | Lab CSV import, in the app and on the command line |

A few tests are marked `xfail`: each one records a place where the code and its docstring disagree, waiting on a decision about which is right.

**Adding a public endpoint** (one reachable without logging in) means adding it to `PUBLIC_ENDPOINTS` in `tests/test_safety.py` on purpose, or the safety tests fail.

**Refactoring?** `tests/route_snapshot.py` records what every page returns and compares two recordings, so you can show a refactor changed nothing:

```bash
python tests/route_snapshot.py record --db biotracking.db --out /tmp/snap-before
# ...refactor...
python tests/route_snapshot.py record --db biotracking.db --out /tmp/snap-after
python tests/route_snapshot.py compare /tmp/snap-before /tmp/snap-after
```

It works on a copy of the database, never the file you point it at.

## Project structure

```
sardinetracker/
├── app.py                      # Entry point: assembles the app from the modules below and runs it
├── appcore.py                  # The Flask app itself: config, migrations hook, secret key, CSRF, login
├── flaremodel.py               # Shared flare-scoring layer: weights, multi-day context, cycle detection
├── scoring.py                  # Pure scoring primitives (no database, no Flask)
├── reminders.py                # ntfy notifications and scheduled jobs
├── db.py                       # All database operations; idempotent run_migrations() at startup
├── uv_fetcher.py               # UV API integration (Open-Meteo + Visual Crossing)
├── summarize.py                # Deterministic digest of one day's flare context
├── setup.py                    # First-run DB schema and config.json
├── create_user.py              # CLI for creating and listing accounts
├── routes/                     # Pages and API, one module per area of the app
│   ├── admin.py                # Login, registration, settings, help pages, admin
│   ├── api.py                  # Token-authenticated JSON API: health sync, UV ingest, flare status, backups
│   ├── clinical.py             # Labs, ANA, events, medications, clinicians, documents
│   ├── daily.py                # Home page, daily entry, mobile quick log
│   ├── dashboard.py            # Model dashboard (/model), UV lag analysis
│   ├── forecast.py             # Forecast, Forecast Lab and its simulation
│   ├── interventions.py        # Intervention evaluation, birth control history, cycle view
│   ├── portal.py               # Clinician portal (capability-token links)
│   └── reports.py              # Search, CSV exports, clinical report
├── import_apple_health.py      # Apple Health CSV importer (HRV, sleep, wrist temp, daylight)
├── import_cycle.py             # Menstrual cycle Apple Health importer
├── import_labs.py              # Lab results CSV importer with ref-range auto-detection
├── import_tracker.py           # Generic symptom-tracker spreadsheet importer
├── import_backup.py            # Import data from a prior biotracking.db backup file
├── backfill_uv.py              # Historical UV data fetcher (Visual Crossing API)
├── migrate_symptoms.py         # One-off migration: symptom category reorganization
├── migrate_to_multiuser.py     # One-off migration: single-user → multi-user schema
├── analysis_cycle_vs_hrv.py    # One-off analysis: luteal phase vs RMSSD deviation as flare predictors
├── severity_diagnostic.py      # One-off analysis: keyword severity buckets from symptom notes
├── severity_vocab.py           # Severity vocabulary for parsing free-text symptom notes
├── rmssd_flare_rerun.py        # Standalone RMSSD pre-flare pattern analysis (generates PNG)
├── make-manpage.py             # Renders TROUBLESHOOTING.md as the sardinetracker(7) man page
├── README.md                   # This file
├── WHY.md                      # The story behind the project
├── MODEL.md                    # Full flare prediction model documentation (rendered at /model/docs)
├── REMOTE_ACCESS.md            # Reaching your instance from outside the house, and hardening it
├── TROUBLESHOOTING.md          # Symptom-first triage for a deployed instance
├── help.md                     # Help text published at sardinetracker.com/docs (in-app: templates/help.html)
├── CHANGELOG.md                # Dated list of substantive changes
├── CONTRIBUTING.md             # Contributor guidelines
├── COMMERCIAL_LICENSE.md       # Commercial licensing terms (AGPL-3.0 for non-commercial)
├── LICENSE                     # AGPL-3.0
├── requirements.txt            # Python dependencies
├── requirements-dev.txt        # Test dependencies (pytest)
├── pytest.ini                  # Test configuration
├── config.json.example         # Template for config.json
├── config.json                 # User settings & API keys (gitignored)
├── biotracking.db              # SQLite database (gitignored)
├── config/
│   ├── custom_weights.json     # Forecast Lab overrides (gitignored; per-user fallback)
│   └── flare_alert_state.json  # Daily alert rate-limit state (gitignored)
├── backups/                    # Local DB backup snapshots (gitignored)
├── man/sardinetracker.7        # Generated man page
├── site/                       # sardinetracker.com landing page; build-docs.py renders the docs
├── tests/                      # pytest suite
└── templates/
    ├── base.html               # Shared layout + global CSS palette (colors referenced app-wide)
    ├── login.html, register.html
    ├── daily_entry.html, daily_confirm.html
    ├── mobile_base.html, mobile_log.html, mobile_status.html  # Phone-optimized entry flow
    ├── forecast.html           # Daily flare forecast with easter-egg >> link to lab
    ├── timeline.html           # Model dashboard (score attribution), served at /model
    ├── forecast_lab.html       # Weight tuning interface
    ├── forecast_history.html   # Predictions vs actuals, ranked by score gap
    ├── forecast_accuracy.html  # Major/minor recall, missed-majors table, factor signal quality
    ├── forecast_patterns.html  # Pre-flare pattern analysis + RMSSD trajectories
    ├── interventions.html      # Per-medication pre/post evaluation + side-effects log
    ├── hrv.html                # Legacy autonomic view (still on disk, no longer nav-linked)
    ├── cycle.html              # Menstrual cycle calendar
    ├── uv_lag.html             # UV-symptom correlation at 0/1/2/3/4-day lags
    ├── wearable.html           # UV wearable view (prototype): per-sample UV chart + daily dose
    ├── clinical_record.html    # Labs, medications, events, clinicians, ANA, documents
    ├── lab_import_preview.html # CSV lab import review step
    ├── portal_manage.html      # Mint/revoke clinician portal links (/portals)
    ├── portal_*.html           # Clinician-facing read-only record (overview + sections)
    ├── settings.html, admin.html
    ├── help.html               # In-app help page
    ├── search.html
    ├── readme.html             # Renders README.md and MODEL.md in-app
    └── remote_access.html      # Renders REMOTE_ACCESS.md in-app
```

## The docs site

`site/build-docs.py` renders REMOTE_ACCESS.md, TROUBLESHOOTING.md, help.md, FEATURES.md and IMPORTING.md into styled pages for sardinetracker.com/docs. The markdown is the source of truth: never hand-edit the generated HTML in `site/public/docs/`, because every build overwrites it. The script refuses to run anywhere but a checkout of the public repo. Build and deploy steps are in its docstring.

## Companion repos

- **[sardinessync](https://github.com/alaricmoore/sardinessync)**: the iOS app (Apple Health). It used to live in this repo at `ios-health-sync/` and was moved out so the Swift and Flask code could evolve on their own schedules.
- **[sardinesync-android](https://github.com/alaricmoore/sardinesync-android)**: the Android app (Health Connect), including a local mode that runs this tracker on the phone.
