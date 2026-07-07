# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

[1.0.0] — 2026-03-06
Added

Initial public release
Daily symptom and biometric tracking
UV lag correlation analysis
Flare forecasting with model accuracy assessment
HRV and autonomic tracking with medication comparison
Clinical record: labs, ANA, medications, events, clinicians
The Forecast Lab
Clinical report generation with PDF export
Full data export and deletion
Apple Health import
Raspberry Pi remote access support via Tailscale

> **Note:** I forgot this file existed for over a month. Entries from 2.2.0 onward were reconstructed from git history on 2026-04-26, so dates reflect when work landed but groupings are post-hoc.

[2.7.0] — 2026-07-07
Name Standardization
- Renamed the GitHub repo sardine-track → sardinetracker, matching sardinetracker.com (old URLs redirect)
- Standardized every user-facing surface on `sardinetracker` (was a mix of sardine-track, sardinetrack, and sardinetracking across README, app wordmark, script output, and docs)
- Fixed broken Android companion links: the repo is sardinesync-android, not sardinessync-android
- Added OpenGraph/Twitter meta tags and canonical URL to the landing page so shared links unfurl properly

[2.6.0] — 2026-04-26
Public Release Prep
- Split narrative content out of README into WHY.md (README had gotten unwieldy)
- Renamed user-facing surfaces: biotracking → sardinetrack
- Renamed sardines-track → sardine-track throughout docs
- Moved iOS sources out to separate sardinessync repo
- Replaced public URL with `<YOUR_SERVER>` placeholder for safe sharing
- Tightened .gitignore, dropped orphan root files (old DBs, favicons, memory dir)
- Updated help.md, REMOTE_ACCESS.md, MODEL.md (fixed stale RMSSD drop claim, removed leftover merge conflict markers)

[2.5.0] — 2026-04-19
Autonomic View → Interventions & Side Effects
- Completely re-did the autonomic view as an interventions and side effects view
- Added intervention card colors
- Rerouted url names
- Added RMSSD instability metric (continues exploring vagal tone oscillation prior to flare hypothesis)
- Consolidated model forecast accuracy and history under /timeline (added >> easter egg link)
- Model charts: added respiratory, increased size
- Fixed UV scoring and cleaned up stale metrics
- Added extended UV-association with future symptom increase
- Changed pain and fatigue scoring and contributing factors to account for cumulative load
- Added RMSSD and other symptom multipliers in forecast/lab
- Updated forecasting model to better account for UV-trigger prodrome and rebalanced flare severity scale

[2.4.0] — 2026-04-05
Mobile + Backfill
- Added mobile bottom nav + responsive forecast and clinical views
- Added mobile quick log and status pages (Phase 4)
- Replaced RMSSD-only backfill with full metric backfill
- Favicons, Flask endpoint setup
- Fixed RMSSD calculation that was mixing timestamps across heartbeat series (2026-04-15)

[2.3.0] — 2026-04-03
Model Dashboard + MODEL.md
- Replaced timeline with model dashboard — score attribution over time
- Added MODEL.md as the canonical model explainer
- Added 3-day symptom burden + RMSSD baseline deviation to flare score
- Refactored symptom burden to baseline-relative delta
- Added respiratory rate baseline deviation to flare score (section 12)
- Disabled cycle_phase weight in flare score — no predictive signal in current data
- Added sparklines to forecast breakdown + CSV score export
- Fixed data export to include all columns; updated forecast lab manual

[2.2.0] - 2026-05-25
Added

- UV wearable view (`/wearable`) — prototype. Charts per-sample UVA/UVB/UV index from a DIY VEML6075 wrist sensor over selectable windows (24h/1w/1mo/6mo/all), with auto-bucketing as the range grows and a stats panel for mean/peak values
- Per-day UV summary: peak UV index and hours above the moderate threshold (UVI ≥ 3.0) per day — a daily-dose view robust to timestamp drift
- Ingest endpoint `POST /api/uv/ingest` — accepts a bearer-authenticated CSV batch from the device; idempotent (INSERT OR IGNORE on user/boot/ms), filters failed I2C reads, back-anchors timestamps for buffered/stale-boot samples
- `uv_sensor_readings` table added via idempotent `run_migrations()` (no manual migration step)
- Config options for the wearable: `wearable_token` (shared bearer secret) and `wearable_user_id` (target account); endpoint returns a clear error and the view shows no data when unset

Changed

- Nav: medication-evaluation view relabeled "interventions" → "reactions" (desktop and mobile). Route stays `/interventions`; the view is as much about how the body reacts to an intervention — side effects, rebounds, autonomic shifts — as the intervention itself
- Added "wearable" to the nav (desktop and mobile "more" menu)

Documentation

- README: new "UV Wearable — Prototype" section, including the no-RTC timestamp caveat (back-anchoring heuristic, approximate rows shown dimmed)
- README: intervention section reframed as "reactions"; wearable.html added to project structure
- Updated help page with current model info + link to model explainer

[2.2.0] — 2026-03-29
iOS Health Sync + RMSSD
- Added iOS health sync app + SpO2/respiratory rate support
- Added RMSSD (parasympathetic HRV) alongside SDNN
- Added RMSSD aggregate trajectory + flare overlays on HRV charts
- Added health-sync API endpoint for iOS Shortcut auto-ingest (exempt from login redirect, date format validation, values rounded to 2 decimals)
- Added iOS Shortcut instructions to remote access guide, README, and help guide
- Added pre-flare pattern analysis page (with UV index and UV dose)
- Added ER-visit severity, backfill with edit/delete, future date guard
- Fixed period detection: new cycles were being absorbed into previous one
- Extended cumulative UV to 3-day rolling sum; added supplement intervention color

[2.1.1] — 2026-03-10
Polish + Bug Fixes
- Replaced passcode lock with full Flask-Login auth + registration + admin route
- Added settings UI for setup options
- Help guide with markdown-to-HTML rendering, hidden README nav, search
- Forecast lab explainer
- ntfy notice when no observation logged in 16 hours
- Expanded cycle view with VAS and HRV charts; BC tracking and med analytics
- Setup additions: DOB and ntfy fields, bad UV API connection warning
- UV upgrades, lag/forecast updates, flare threshold bug fix
- Weights now affect full model; apply route fixed
- config.json.example, custom_weights.json, .gitignore cleanup, removed .venv from tracking
- Download/delete bug fixes

[2.1.0] - 2026-03-07

- Added Cycle Phase Flare Weighting
- Added _inject_cycle_phase() and_compute_phase_by_date_from_obs() helpers — annotates observation dicts with cycle_in_high_risk_phase (bool) and cycle_phase_name using BBT-anchored ovulation detection
- Added cycle_phase: 1.0 to DEFAULT_WEIGHTS — PMS/luteal phase contributes +1.0 to flare score (adjustable in Forecast Lab, no-op when track_cycle: false)
- Updated calculate_flare_prime_score(), get_contributing_factors(), get_score_breakdown() to include cycle phase
- Added cycle phase slider to Forecast Lab (/forecast/lab) — only visible when track_cycle: true
- Called _inject_cycle_phase() in 6 forecast routes so phase is always annotated before scoring
- Performance: recall improved 36.7% → 45.5%, false positives reduced 20% → 16.7%
- Proactive Flare Risk Alerts (ntfy)
- Added_send_ntfy_alert() — separate from medication reminder sender, supports priority and tags
- Added _check_flare_risk_alert() — daily cron at configurable hour (default 8am), sends alert when weighted 3-day score ≥ 5.0 or when entering PMS/luteal phase tomorrow
- High-risk alerts (≥ 8.0) use high priority and rotating_light tag
- Rate-limited to once per calendar day via config/flare_alert_state.json
- Config options: flare_alert_hour (default 8), disable by clearing ntfy_topic
- Added to .gitignore: config/flare_alert_state.json
- Quick Entry Mode
- Added ?mode=quick URL param to /daily — shows only fields that feed the prediction model (pain, fatigue, emotional state, symptom checkboxes, flare flags, period flow)
- All hidden fields carry existing values as hidden inputs so data isn't wiped on save
- Symptom notes stripped in quick mode (checkboxes only, no expandable text areas)
- Mode toggle link shown near page subtitle
- Clinical Report Improvements
- Clinical Summary: Added flare count for the period and date of most recent flare
- Symptom Frequency table: New section, days each category flagged out of total tracked days, sorted by frequency, ≥30% highlighted
- ANA — Positive Results (All Time): New section showing only non-negative ANA results with titer, pattern(s), and provider; includes clinical note explaining why negatives are excluded (ANA fluctuates in  early disease)
- Print header: Print-only div with patient name, period, generated date, primary intervention — hidden on screen, visible in PDF
- Richer auto-findings: generate_findings() now produces flare frequency finding, highest-burden symptom category, neurological involvement flag (≥10% of days), medication-started-during-period findings
- Typo fix: "Encounter's & Events" → "Encounters & Events"
- Security Hardening
- Debug off by default: debug=True replaced with debug=CONFIG.get('debug', False) — enable with "debug": true in config.json
- SECRET_KEY: Auto-generated 32-byte hex key written by setup.py to config.json; fallback warning printed if missing from an existing install
- Optional passcode lock: Add "passcode": "yourpin" to config.json to require login — session-based, before_request guard on all routes, login/logout routes, lock button in nav. No effect when key is absent.
- CSRF protection: flask-wtf>=1.2.0 added; CSRFProtect(app) initialized; CSRF meta tag in base.html; JS auto-injector adds token to all POST forms at DOM load; window.csrfFetch() helper for fetch-based POST  calls; all three template fetch calls updated to csrfFetch()
- New template: templates/login.html — passcode form using existing design system
- requirements.txt: Added flask-wtf>=1.2.0
- Documentation
- README: Cycle tracker section, luteal phase weighting rationale with real data
- README: Push notifications section expanded (flare alerts, timing config, rate limiting)
- README: Performance numbers updated (recall 36.7% → 45.5%, false positives 20% → 16.7%)
- README: "From Apple Health — Menstrual Cycle Data" import section added
- README: Optional Passcode section with step-by-step setup instructions
- README: login.html added to project structure
- README: Data Privacy & Security bullet added for passcode feature
