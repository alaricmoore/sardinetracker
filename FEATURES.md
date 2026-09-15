# Features — everything sardinetracker does

The full feature list, one area at a time. For installing, see the [README](README.md). For how to use each part day to day, see [help.md](help.md). For how the flare score is calculated, see [MODEL.md](MODEL.md).

## Daily Tracking

- **Symptom logging**: Track 9 symptom categories with detailed notes (neurological, cognitive, musculature, migraine, pulmonary, dermatological, mucosal, rheumatic, gastrointestinal)
- **Environmental factors**: UV exposure (pulled from Open-Meteo and Visual Crossing), temperature, sleep quality
- **Physical metrics**: Steps, basal body temperature, pain/fatigue scales (1-10)
- **Flare documentation**: Mark flare days and track what actually happened
- **Quick entry mode**: Stripped-down form (`?mode=quick`) showing only the fields that feed the prediction model — for days when filling a full form is too much

## Menstrual Cycle Tracking (Optional)

Enabled via `track_cycle: true` in setup. Designed for patients where steroids, biologics, or disease activity disrupt menstrual regularity.

- **Month-grid calendar**: Color-coded period flow (heavy/medium/light/spotting) with phase overlays
- **BBT-anchored phase detection**: Uses basal body temperature biphasic shift (rule of three: 3 consecutive days ≥ 0.1°F above follicular average) to detect actual ovulation rather than relying on fixed calendar math — critical when steroids compress or extend cycle length unpredictably
- **Cycle phase overlays**: Luteal and PMS windows calculated from detected ovulation, not assumed 14-day countdown
- **BBT heat visualization**: Per-day colored border on calendar cells indicating BBT elevation, colored SVG wave graph per month
- **Flare/phase correlation**: Patterns card showing flare distribution by cycle phase across all historical data
- **Intervention effects on cycle**: Before/after average cycle length for each medication intervention
- **Apple Health import**: `import_cycle.py` imports Menstrual Flow and Intermenstrual Bleeding records from Apple Health export CSV

## Data Visualization & Analysis

- **Model dashboard** (`/model`, nav label "model"): score attribution over time — stacked bars showing exactly what's driving each day's risk score, with flare markers and threshold line. Includes click-to-expand trend charts for symptom burden delta, RMSSD deviation, and respiratory rate deviation (with +10% / +15% scoring thresholds dashed in). Score distribution stats (flare vs non-flare) and per-day prediction accuracy strip.
- **Model sub-navigation from the dashboard**: accuracy, history, and pre-flare patterns are all hyperlinked from the model view so you don't have to dig through nested menus. A hidden `>>` chevron in the corner opens the Forecast Lab (easter-egg style,just because that's fun).
- **Pre-flare pattern analysis** (`/forecast/patterns`): biometric averages, symptom frequencies, and RMSSD trajectories in the days before ER visits and major flares, with aggregate mean lines and confidence bands.
- **UV lag analysis** (`/uv-lag`): Pearson correlation between UV dose and each symptom at lag windows of 0, 1, 2, 3, and 4 days — identifies your personal best-predicting UV-to-symptom delay.
- **Intervention evaluation** (`/interventions`, nav label "reactions"): per-medication pre/post flare and autonomic shift analysis, plus structured logging of how your body *reacts* to each intervention (side effects, rebounds, dose changes). Described in its own section below — it's a big enough feature to deserve one.
- **UV wearable** (`/wearable`, nav label "wearable") — *prototype*: per-sample UV exposure from a DIY wearable sensor, charted over time with per-day peak/dose summaries. Experimental; see the dedicated section below.

## Flare Prediction Model

Transparent statistical scoring, tunable per-user, with 13 contributing categories:

- **Environmental**: UV dose (same-day + 4-day cumulative with flattened decay)
- **Physiological**: Physical overexertion (steps relative to personal baseline, sleep-adjusted), basal body temperature delta
- **Symptoms**: 8 symptom categories with per-category weights, plus rheumatic joint-location parsing (major joints score higher than minor)
- **Pain / Fatigue / Emotional**: laddered scoring (pain ≥4/5/6/7 and fatigue ≥4/5/6/7 step up in contribution — replaces the old cliff-at-7 threshold that missed function-limiting days where a single severe symptom was the whole event)
- **Symptom burden delta**: how many more symptoms are active than your personal 14-day baseline — "symptoms accelerating above your normal" rather than raw count, so chronic daily symptoms don't drown out the signal
- **RMSSD baseline deviation**: 7-day rolling vagal tone vs 30-day baseline. Drops before and during flares, mechanistically grounded in the cholinergic anti-inflammatory pathway, empirically replicates Thanou 2016's ΔRMSSD-ΔSLEDAI finding in n=1 data.
- **RMSSD instability**: mean day-to-day |ΔRMSSD| over prior 5 days vs 30-day baseline. Captures autonomic *chaos* — the pattern where RMSSD oscillates wildly in the week before major flares rather than just trending down. Independent additive signal to the level-based deviation above.
- **Respiratory rate baseline deviation**: 3-day rolling respiratory rate vs 14-day baseline. Motivated by ICU deterioration literature (Barfod 2017) but labeled with an honest caveat that the n=1 cross-sectional signal is weak — the `/model` resp-rate chart is the live validation tool.
- **Real-time risk assessment**: daily flare risk score (0-25) with color-coded risk levels; separate major-flare recall tracked as the primary performance metric because function-limiting flares are the ones that matter most to catch.
- **Contributing factors breakdown**: every score surfaces which rules fired and how many points each contributed — nothing is hidden.
- **Personalized recommendations**: context-aware suggestions based on current risk level.

## Forecast Lab (model tuning)

- **Interactive weight adjustment**: tune symptom weights, category multipliers, multi-day predictor weights, and the flare threshold using real-time sliders. All sliders are exposed — no hidden tunables.
- **Live simulation**: see how weight changes affect accuracy, recall, and precision before committing.
- **Prediction flip analysis**: identify which dates would change prediction with new weights.
- **Apply and revert**: save custom weights per-user or reset to factory defaults.
- **Personal lag signature widget**: shows which UV-to-symptom lag correlates strongest in your data.
- **Built-in manual**: accessible via `?` command in the terminal-style interface.
- **Model transparency**: the actual Python calculation code is rendered inline in the app via `inspect.getsource()` — the code you see is the code that runs.

## Model Performance Tracking

- **Major flare recall as headline metric**: major function-limiting flares are the ones you can't afford to miss. Separate recall figures for major/ER, minor, and combined.
- **Accuracy analysis** over 30/60/90/120/365/all day windows.
- **Confusion matrix**: true positives, false positives, true negatives, false negatives.
- **Full ranked missed-majors table**: every major flare the model missed, sorted by how far below threshold the score fell, with your notes and fired factors inline so you can see the context.
- **Factor signal quality table**: for each scoring factor, compares fire-rate on caught majors vs missed majors — positive lift means the factor is earning its weight, low or negative lift flags factors that aren't helping.
- **Historical validation**: compare predictions against actual flare outcomes with clickable dates that jump to the daily entry for context.
- **Weight optimization**: data-driven suggestions for improving model performance based on severity-aware false-positive and false-negative patterns.

## Intervention Tracking & Side Effects (`/interventions`, nav label "reactions")

Purpose-built clinical evaluation view: "did this medication actually help?" in stats rather than generalizations. (The route is still `/interventions`; the nav now labels it **reactions** because the view is as much about how your body *reacts* to an intervention — side effects, rebounds, autonomic shifts — as about the intervention itself.)

- **Per-medication cards**: one card for each medication flagged as primary or secondary intervention (toggle the flag in `/clinical#medications`). Shows pre/post:
    - **Flare impact**: total count, major/ER count, minor count, mean gap days, delta percentages color-coded by direction
    - **Autonomic shift**: RMSSD / SDNN / resp rate means ± SD, with n counts
- **Matched-window analysis** for ongoing medications: if HCQ has been running 130 days, it compares the 130 days before you started to the 130 days of use — statistically honest rather than an arbitrary fixed window
- **Fixed-window analysis** for one-time doses (steroid injections, IV's, etc.): 30/60/90/120/all-day selector
- **Duration-of-effect for one-time interventions**: days to next flare per severity tier; days until each autonomic metric returned to baseline (within ±1 SD of pre-mean for 7 consecutive days)
- **Auto-detected rebound flags**: if a one-time dose reduced flare rate initially (days 0-13 post) but rate surged back in days 14-45, an amber banner surfaces the pattern. Helpful for catching IV steroid rebounds you might miss manually.
- **Events log per medication**: structured dated observations with types `side_effect` / `rebound` / `efficacy_change` / `dose_change` / `note`. Side effects get severity 0-10; other types skip the severity field.
- **Global HRV trend chart** at the top of the page with intervention start lines and flare markers overlaid — the "am I trending up overall" glance kept from the old `/autonomic` view.
- **Color-coded cards**: primary interventions get a teal left-border, secondary get purple, supplements get orange. Stat boxes inside each card use the global palette (RMSSD purple, SDNN blue, resp teal, flare red, minor amber) so the eye tracks consistent colors across the app.

## UV Wearable (`/wearable`) — Prototype

> **Status: experimental.** This is an optional, in-development feature for a DIY hardware add-on. The core tracker works fully without it. The hardware/firmware live in a separate project; what ships here is the server side — an ingest endpoint and a view. Treat the timestamps as approximate (see the caveat below).

The motivation is the lupus use case: UV is a known photo-trigger, and a wearable sensor measures *your actual exposure* at the body rather than the regional forecast that feeds the rest of the app's UV scoring. The two are complementary — forecast UV for prediction, measured UV for ground truth.

- **The device**: a small unit with a VEML6075 UV sensor (UVA/UVB/IR channels), sampling roughly every 5 minutes and buffering readings until it can reach the server.
- **Ingest** (`POST /api/uv/ingest`): the device uploads a CSV batch (one row per sample or event mark) authenticated with a bearer token. Rows are de-duplicated, so re-syncing overlapping data is safe. Bad I2C reads (a floating bus that pegs one channel to `0xFFFF`) are filtered out.
- **`/wearable` view**: charts per-sample UVA, UVB, and computed UV index over a selectable window (24h / 1 week / 1 month / 6 months / all time), with the chart auto-bucketing to coarser bins as the range grows. A stats panel shows mean and peak values.
- **Per-day summary**: peak UV index and **hours above the moderate threshold** (UVI ≥ 3.0, the WHO "sunburn risk for unprotected skin" line) for each day — a daily-dose view that's more robust than the chart to timing error.
- **The timestamp caveat (important)**: the prototype device has no real-time clock. Samples taken while the device is connected are anchored to real time from the sync; samples buffered across reboots are *back-anchored* by chaining each boot's observed duration backward from the most recent sync. This is a heuristic — if the device sleeps or charges off between boots, those gaps get swallowed and older samples drift later than reality. Approximate rows are shown dimmed/small and can be hidden with `?include_approx=0`. The per-day peak/dose summaries tolerate this better than the minute-resolution chart does.

**Setup** (only if you've built the device): add `wearable_token` (the shared bearer secret) and `wearable_user_id` (which account the readings belong to) to `config.json`. With those unset, the ingest endpoint returns a clear error and the view simply shows no data.

## Clinical Record Management

- **Lab results**: Track test results with numeric values, qualitative results, reference ranges, and flags 
- **Medications**: Full medication history with doses, frequencies, start/end dates, and primary/secondary intervention flags (the flags feed the `/interventions` view)
- **Clinical events**: Document appointments, procedures, hospitalizations with provider and facility info
- **Clinician directory**: Maintain contact info for your care team (specialty, clinic, network, notes)
- **ANA tracking**: Specialized tracking for ANA titers, patterns, and screen results
- **CSV export**: Export labs, medications, events, or clinician data for external analysis or records requests
- **Steroid taper wizard**: Pre-filled 6-day Medrol dose pack schedule with adjustable times and doses; schedules push notifications for each dose via ntfy
- **Dose checklist**: Today's scheduled doses appear on the daily entry page with one-tap "mark taken" tracking
- **Document library**: Upload clinic notes, pathology, and imaging PDFs to a documents tab; text is extracted (pdftotext or pypdf) so documents are full-text searchable. Files stay on your disk — only metadata and searchable text go in the database
- **CSV lab import**: Paste or upload a CSV of lab results in-app, get an auto-flagged preview (reference ranges detected, duplicates tagged), and commit only the rows you check

## Clinician Portal (share your record)

Bring a clinician into your data without handing over an account, an export, or a stack of paper.

- **Capability links**: mint a read-only link tied to a specific clinician from `/portals` — the URL itself is the key, nothing to install or sign into on their end
- **Overview page**: a report-style synopsis (flare burden, disease-modifying therapy, serology, mean pain/fatigue), the disease-burden chart, and auto-generated findings up top, then a card for each section of the record
- **Section pages**: documents, medications (current + past), labs (curated key markers, complete history, ANA), clinical timeline, and 90-day symptom history
- **Source documents included**: every uploaded PDF opens directly through the link, so the clinician can check the primary source, not just your summary
- **Patient-controlled**: links expire (30 days by default), can be revoked at any time, and every access is logged so you can see when and what was viewed
- **Print-ready**: every portal page (and the in-app report) reprints as clean ink-on-paper for clinicians who want a hard copy

## Search & Navigation

- **Full-text search**: Search across all daily entries, clinical notes, medications, and events
- **Keyword shortcuts**: Type "help", "manual", "lab", "cli" in search to access Forecast Lab
- **Recent note reference**: Access previous notes and events by keyword
- **Quick filters**: Jump to specific symptom categories or date ranges

## Data Privacy & Security

- **Local-first**: All data stored in local SQLite database on your machine
- **No cloud sync**: Data never leaves your computer by default
- **Accounts and login**: every page sits behind a username and password (bcrypt-hashed); each account's records are kept separate. See [Accounts and Login](README.md#accounts-and-login) in the README.
- **Optional remote access**: reach your instance from outside the house through a Cloudflare Tunnel or a Tailscale-connected VPS, with a checklist for hardening the app before it faces the internet (see [REMOTE_ACCESS.md](REMOTE_ACCESS.md))
- **Version control safe**: Comprehensive `.gitignore` protects health data from accidental commits
- **Export control**: You decide what data leaves your system and when

## Technical Features

- **UV auto-backfill**: Automatically fetch historical UV data from Open-Meteo and Visual Crossing based on GPS coordinates
- **Responsive design**: Works on desktop and mobile browsers
- **Dark mode**: Easy on the eyes, just toggle the moon/sun in the header.
- **Light mode**: Good for when you can't make out dark mode.
