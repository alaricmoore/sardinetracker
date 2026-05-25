# sardine-track (biotracking)

A local-only health tracking application for patients navigating complex diagnostic journeys.

Built for people who need to see patterns in their own data when the medical system isn't connecting the dots yet.

(Well, built for one person who needed to see patterns, but she figured she couldn't be the only nut out there.)

> **Note:** This is the public codebase of what was originally released as `biotracking`, now named **sardine-track** (a SARDs pun — "systemic autoimmune rheumatic disease," plus a tracker that sticks with you). The GitHub rename keeps the old URL as a redirect, so existing links still work. Active experimentation continues in a private fork that may push features back upstream when they prove out. The iOS companion app lives in its own repo: **[sardinessync](https://github.com/alaricmoore/sardinessync)**. Want to know how your data is stored and how it gets to your phone? See the [remote access guide](REMOTE_ACCESS.md).
>
> **About the family-instance framing:** the app supports multiple users and was originally built hoping family with shared genetic risk might want to track alongside. In practice the daily-entry burden has kept adoption to one. Multi-user plumbing is preserved — if a family member or friend does decide to try, they can register their own account on the same instance without affecting anyone else's data.

---

## What It Does

Sardine-track helps you:

- Track daily symptoms, biometrics, and environmental factors (including UV exposure, in fact especially UV exposure)
- Visualize correlations over time (does UV exposure predict your symptom flares? does low HRV precede bad days?)
- Generate clinical reports to bring to appointments (when you know damn well your brain is not going to remember everything, plus it has graphs!)
- Keep a longitudinal record of labs, medications, & clinical events, as well a list of your clinicians
- Run flare forecasting based on your own historical patterns — transparent scoring, not a black box, and tuned on your own n=1 data
- Evaluate medical interventions (hydroxychloroquine, steroids, biologics, whatever): per-medication pre/post flare impact, autonomic shift, duration-of-effect for one-time doses, and structured logging of side effects, rebounds, and dose changes
- Auto-sync biometrics from Apple Health via the **[sardinessync](https://github.com/alaricmoore/sardinessync)** iOS companion app (steps, HRV/SDNN, RMSSD, resting heart rate, SpO2, respiratory rate, basal body temperature, time in daylight)
- Keep all your data local — nothing leaves your computer, if you don't want it to.

This is not a medical product. This is a tool for veracity: for people who need to make their invisible patterns visible, whether for themselves or to make a case to others.

---

## Features

### Daily Tracking

- **Symptom logging**: Track 9 symptom categories with detailed notes (neurological, cognitive, musculature, migraine, pulmonary, dermatological, mucosal, rheumatic, gastrointestinal)
- **Environmental factors**: UV exposure (pulled from Open-Meteo and Visual Crossing), temperature, sleep quality
- **Physical metrics**: Steps, basal body temperature, pain/fatigue scales (1-10)
- **Flare documentation**: Mark flare days and track what actually happened
- **Quick entry mode**: Stripped-down form (`?mode=quick`) showing only the fields that feed the prediction model — for days when filling a full form is too much

### Menstrual Cycle Tracking (Optional)

Enabled via `track_cycle: true` in setup. Designed for patients where steroids, biologics, or disease activity disrupt menstrual regularity.

- **Month-grid calendar**: Color-coded period flow (heavy/medium/light/spotting) with phase overlays
- **BBT-anchored phase detection**: Uses basal body temperature biphasic shift (rule of three: 3 consecutive days ≥ 0.1°F above follicular average) to detect actual ovulation rather than relying on fixed calendar math — critical when steroids compress or extend cycle length unpredictably
- **Cycle phase overlays**: Luteal and PMS windows calculated from detected ovulation, not assumed 14-day countdown
- **BBT heat visualization**: Per-day colored border on calendar cells indicating BBT elevation, colored SVG wave graph per month
- **Flare/phase correlation**: Patterns card showing flare distribution by cycle phase across all historical data
- **Intervention effects on cycle**: Before/after average cycle length for each medication intervention
- **Apple Health import**: `import_cycle.py` imports Menstrual Flow and Intermenstrual Bleeding records from Apple Health export CSV

### Data Visualization & Analysis

- **Model dashboard** (`/model`, nav label "model"): score attribution over time — stacked bars showing exactly what's driving each day's risk score, with flare markers and threshold line. Includes click-to-expand trend charts for symptom burden delta, RMSSD deviation, and respiratory rate deviation (with +10% / +15% scoring thresholds dashed in). Score distribution stats (flare vs non-flare) and per-day prediction accuracy strip.
- **Model sub-navigation from the dashboard**: accuracy, history, and pre-flare patterns are all hyperlinked from the model view so you don't have to dig through nested menus. A hidden `>>` chevron in the corner opens the Forecast Lab (easter-egg style,just because that's fun).
- **Pre-flare pattern analysis** (`/forecast/patterns`): biometric averages, symptom frequencies, and RMSSD trajectories in the days before ER visits and major flares, with aggregate mean lines and confidence bands.
- **UV lag analysis** (`/uv-lag`): Pearson correlation between UV dose and each symptom at lag windows of 0, 1, 2, 3, and 4 days — identifies your personal best-predicting UV-to-symptom delay.
- **Intervention evaluation** (`/interventions`, nav label "reactions"): per-medication pre/post flare and autonomic shift analysis, plus structured logging of how your body *reacts* to each intervention (side effects, rebounds, dose changes). Described in its own section below — it's a big enough feature to deserve one.
- **UV wearable** (`/wearable`, nav label "wearable") — *prototype*: per-sample UV exposure from a DIY wrist sensor, charted over time with per-day peak/dose summaries. Experimental; see the dedicated section below.

### Flare Prediction Model

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

### Forecast Lab (model tuning)

- **Interactive weight adjustment**: tune symptom weights, category multipliers, multi-day predictor weights, and the flare threshold using real-time sliders. All sliders are exposed — no hidden tunables.
- **Live simulation**: see how weight changes affect accuracy, recall, and precision before committing.
- **Prediction flip analysis**: identify which dates would change prediction with new weights.
- **Apply and revert**: save custom weights per-user or reset to factory defaults.
- **Personal lag signature widget**: shows which UV-to-symptom lag correlates strongest in your data.
- **Built-in manual**: accessible via `?` command in the terminal-style interface.
- **Model transparency**: the actual Python calculation code is rendered inline in the app via `inspect.getsource()` — the code you see is the code that runs.

### Model Performance Tracking

- **Major flare recall as headline metric**: major function-limiting flares are the ones you can't afford to miss. Separate recall figures for major/ER, minor, and combined.
- **Accuracy analysis** over 30/60/90/120/365/all day windows.
- **Confusion matrix**: true positives, false positives, true negatives, false negatives.
- **Full ranked missed-majors table**: every major flare the model missed, sorted by how far below threshold the score fell, with your notes and fired factors inline so you can see the context.
- **Factor signal quality table**: for each scoring factor, compares fire-rate on caught majors vs missed majors — positive lift means the factor is earning its weight, low or negative lift flags factors that aren't helping.
- **Historical validation**: compare predictions against actual flare outcomes with clickable dates that jump to the daily entry for context.
- **Weight optimization**: data-driven suggestions for improving model performance based on severity-aware false-positive and false-negative patterns.

### Intervention Tracking & Side Effects (`/interventions`, nav label "reactions")

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

### UV Wearable (`/wearable`) — Prototype

> **Status: experimental.** This is an optional, in-development feature for a DIY hardware add-on. The core tracker works fully without it. The hardware/firmware live in a separate project; what ships here is the server side — an ingest endpoint and a view. Treat the timestamps as approximate (see the caveat below).

The motivation is the lupus use case: UV is a known photo-trigger, and a wrist-worn sensor measures *your actual exposure* at the body rather than the regional forecast that feeds the rest of the app's UV scoring. The two are complementary — forecast UV for prediction, measured UV for ground truth.

- **The device**: a small wrist unit with a VEML6075 UV sensor (UVA/UVB channels), sampling roughly every 5 minutes and buffering readings until it can reach the server.
- **Ingest** (`POST /api/uv/ingest`): the device uploads a CSV batch (one row per sample or event mark) authenticated with a bearer token. Rows are de-duplicated, so re-syncing overlapping data is safe. Bad I2C reads (a floating bus that pegs one channel to `0xFFFF`) are filtered out.
- **`/wearable` view**: charts per-sample UVA, UVB, and computed UV index over a selectable window (24h / 1 week / 1 month / 6 months / all time), with the chart auto-bucketing to coarser bins as the range grows. A stats panel shows mean and peak values.
- **Per-day summary**: peak UV index and **hours above the moderate threshold** (UVI ≥ 3.0, the WHO "sunburn risk for unprotected skin" line) for each day — a daily-dose view that's more robust than the chart to timing error.
- **The timestamp caveat (important)**: the prototype device has no real-time clock. Samples taken while the device is connected are anchored to real time from the sync; samples buffered across reboots are *back-anchored* by chaining each boot's observed duration backward from the most recent sync. This is a heuristic — if the device sleeps or charges off between boots, those gaps get swallowed and older samples drift later than reality. Approximate rows are shown dimmed/small and can be hidden with `?include_approx=0`. The per-day peak/dose summaries tolerate this better than the minute-resolution chart does.

**Setup** (only if you've built the device): add `wearable_token` (the shared bearer secret) and `wearable_user_id` (which account the readings belong to) to `config.json`. With those unset, the ingest endpoint returns a clear error and the view simply shows no data.

### Clinical Record Management

- **Lab results**: Track test results with numeric values, qualitative results, reference ranges, and flags 
- **Medications**: Full medication history with doses, frequencies, start/end dates, and primary/secondary intervention flags (the flags feed the `/interventions` view)
- **Clinical events**: Document appointments, procedures, hospitalizations with provider and facility info
- **Clinician directory**: Maintain contact info for your care team (specialty, clinic, network, notes)
- **ANA tracking**: Specialized tracking for ANA titers, patterns, and screen results
- **CSV export**: Export labs, medications, events, or clinician data for external analysis or records requests
- **Steroid taper wizard**: Pre-filled 6-day Medrol dose pack schedule with adjustable times and doses; schedules push notifications for each dose via ntfy
- **Dose checklist**: Today's scheduled doses appear on the daily entry page with one-tap "mark taken" tracking

### Search & Navigation

- **Full-text search**: Search across all daily entries, clinical notes, medications, and events
- **Keyword shortcuts**: Type "help", "manual", "lab", "cli" in search to access Forecast Lab
- **Recent note reference**: Access previous notes and events by keyword
- **Quick filters**: Jump to specific symptom categories or date ranges

### Data Privacy & Security

- **Local-first**: All data stored in local SQLite database on your machine
- **No cloud sync**: Data never leaves your computer by default
- **Optional passcode lock**: Require a PIN to access the app — useful on shared networks, with roommates, or in any situation where you need your health data to stay private from people in your physical space. Enable by adding one line to `config.json`. See [Optional Passcode](#optional-passcode-access-control) below.
- **Optional remote access**: Raspberry Pi + Tailscale + Oracle Cloud setup for secure mobile access (see REMOTE_ACCESS.md)
- **Version control safe**: Comprehensive `.gitignore` protects health data from accidental commits
- **Export control**: You decide what data leaves your system and when

### Technical Features

- **UV auto-backfill**: Automatically fetch historical UV data from Open-Meteo and Visual Crossing based on GPS coordinates
- **Responsive design**: Works on desktop and mobile browsers
- **Dark mode**: Easy on the eyes, just toggle the moon/sun in the header.
- **Light mode**: Good for when you can't make out dark mode.

## Important Disclaimers

### Not Medical Advice

This application is a data tracking and visualization tool only. It is not:

- A diagnostic tool
- Medical advice
- A replacement for professional medical care
- Approved, endorsed, or reviewed by any medical authority

Always consult qualified healthcare providers for medical decisions. This app helps you organize your own observations -- what you do with that information is between you and your clinicians.

### Privacy & Data Ethics

- Your data never leaves your computer. No cloud storage, no third-party APIs for health data, no analytics, no tracking.
- UV data comes from public weather APIs (Open-Meteo and Visual Crossing) using only your coordinates — no personal health information is transmitted.
- You own your data. The database is a standard SQLite file you can back up, export, or delete at any time.
- This is a single-user, local application. One instance per person, one database per instance.
- Do not use this application to track anyone's health data without their informed consent. Don't be creepy.

---

## Requirements

- macOS, Linux, or Windows (tested primarily on macOS and Linux... actually not tested on Windows. Sorry.)
- Python 3.9 or later (earlier veersions work, but watch your D's and d's)
- A web browser (Brave, Firefox, Safari, Edge, Opera, Tor...)
- Optional: iPhone with Apple Health for biometric import (I have an apple watch, because access to raw data for free and it's also a watch)

---

## Installation

### Step 1: Install Python

**macOS/Linux:** Python 3 is likely already installed. Open Terminal and check:

```bash
python3 --version
```

If you see Python 3.9 or higher, you're good. If not, download from [python.org](https://python.org).

**Windows:** Download Python from [python.org](https://python.org) and make sure to check "Add Python to PATH" during installation.

### Step 2: Download sardine-track

**Option A: Download ZIP (easiest if you're not familiar with git)**

1. Go to the GitHub repository page
2. Click the green **Code** button
3. Click **Download ZIP**
4. Unzip the file to a folder you can find (like `Documents/sardine-track`)

**Option B: Clone with git**

```bash
git clone https://github.com/alaricmoore/sardine-track.git
cd sardine-track
```

> The repo was formerly named `biotracking`; the old URL still redirects. If you want the iOS companion as well, the Swift sources live in a separate repo: [github.com/alaricmoore/sardinessync](https://github.com/alaricmoore/sardinessync).

### Step 3: Set Up the Application

Open Terminal (Mac/Linux) or Command Prompt (Windows), navigate to the sardine-track folder, and run:

```bash
# Create a virtual environment (recommended)
python3 -m venv .venv

# Activate it
# Mac/Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run first-time setup
python setup.py
```

The setup script will ask you for:

- Your name (for reports)
- Location coordinates (for UV data — you can find these by Googling "my coordinates" or using [latlong.net](https://latlong.net))
- Timezone (e.g., `America/Chicago`, `America/New_York`, `Europe/London`)
- Baseline body temperature in Fahrenheit (your normal resting temp, usually around 97-99°F)

> **Important for coordinates:** If you're in North America, your longitude should be negative. For example, Oklahoma City is `35.4676, -97.5164` (note the minus sign on longitude). The setup script will warn you if you forget.

### Step 4: Start the Application

```bash
python app.py
```

You should see:

```
sardinetrack
============
Patient: Your Name
Starting server...

Local:  http://localhost:5000
Phone:  connect to same wifi, visit http://<your-ip>:5000
```

> **A note on the name:** internally, the code still calls itself `biotracking` in a lot of places (module docstrings, the `biotracking.db` filename, some comments). That was the project's original name before it became `sardine-track`. It's left alone on purpose — renaming every occurrence is churn without benefit, and the database file in particular would break existing installs if renamed. User-facing surfaces (this banner, script output, `--help` text) say `sardinetrack`.

Open your browser and go to `http://localhost:5000`. Try adding today's entry to make sure everything works.

---

## Accessing from Your Phone

If you want to enter data from your phone while on the same WiFi network:

**Find your computer's IP address:**

- **macOS:** System Settings > Network > click your connection > look for IP Address
- **Windows:** Open Command Prompt, type `ipconfig`, look for "IPv4 Address"
- **Linux:** Run `hostname -I`

Then on your phone (same WiFi network), open a browser and go to `http://YOUR-IP-ADDRESS:5000`.

Bookmark it for easy access.

---

## Importing Your Data

### From Apple Health

Biotracking can import HRV, sleep hours, wrist temperature, and daylight exposure from Apple Health, or whatever else you are tracking. Which provides free-of-cost raw data downloads.

**Export from Apple Health:**

1. Open the Health app on your iPhone
2. Tap your profile picture in the top right
3. Scroll down and tap **Export All Health Data**
4. AirDrop the export to your Mac, or save to Files and transfer via iCloud
5. The file is going to be huge, just a warning.

**Recommended: Use the Health Export app (free tier is fine)**

1. Download [Health Export](https://apps.apple.com/app/health-export/id1477722520) from the App Store
2. Select: Heart Rate Variability, Sleep Analysis, Apple Sleeping Wrist Temperature, Time in Daylight
3. Set your date range, export as CSV daily average
4. Transfer the CSV to your computer
5. Also download Menstrual Cycle data if you're using cycle tracking — see below.

**Import:**

```bash
python import_apple_health.py path/to/your_export.csv

# Preview without writing:
python import_apple_health.py ~/Downloads/health_export.csv --dry-run

# Create new rows for dates that don't exist yet:
python import_apple_health.py ~/Downloads/health_export.csv --create-new
```

### From Apple Health — Menstrual Cycle Data

If you enabled cycle tracking during setup, import your cycle history from an Apple Health XML export:

1. Export from Health app (see above)
2. Use the [Health Export app](https://apps.apple.com/app/health-export/id1477722520) — select **Menstrual Flow** and **Intermenstrual Bleeding**, export as CSV
3. Import:

```bash
# Preview first:
python import_cycle.py --csv your_cycle_export.csv --dry-run

# Import:
python import_cycle.py --csv your_cycle_export.csv
```

Flow priority when multiple records exist for the same day: heavy > medium > light > spotting. Intermenstrual Bleeding is imported as spotting. Sexual Activity and Persistent Menstrual Bleeding records are skipped.

### From Your Own Symptom Tracker

If you've been tracking in a spreadsheet, you can import it. Required column: `Date` (in YYYY-MM-DD, MM/DD/YYYY, or "Jul 22, 2025" format). Optional columns are mapped automatically for symptom flags, pain/fatigue scales, sleep hours, and notes.

```bash
python import_tracker.py path/to/your_tracker.csv --dry-run
python import_tracker.py path/to/your_tracker.csv
```

### Lab Results

```csv
Date,Test,Value,Units,Lab,Doctor
2021-04-16,C4,28,mg/dL,LabCorp,Dr. Smith
```

```bash
python import_labs.py path/to/labs.csv --dry-run
python import_labs.py path/to/labs.csv
```

The script auto-detects reference ranges and flags common tests (C3, C4, CRP, ESR, anti-dsDNA, etc.).

### UV Data Backfill

After importing historical data, fetch UV values for those dates:

```bash
python backfill_uv.py
```

You'll need a free [Visual Crossing](https://visualcrossing.com) API key. Add it to `config.json`:

```json
"visual_crossing_key": "YOUR_KEY_HERE"
```

> The free tier allows 1000 records/day. Historical UV uses ~24 records per day, so you can backfill about 40 days for free. Beyond that, the metered plan is $0.0001/record.

---

## Usage

### Daily Workflow

1. Open biotracking at `http://localhost:5000`
2. Click "Daily Entry" to log today's data
3. Fill in symptoms, environmental factors, and notes
4. Check "Flare occurred today" if applicable
5. Submit to save

### Checking Your Flare Risk

1. Navigate to "Forecast" from the main menu for the current-day risk score and recommendations.
2. Navigate to "Model" (`/model`) for the score attribution dashboard — stacked-bar breakdown of what's driving each day, plus click-to-expand trend charts for burden delta, RMSSD deviation, and respiratory rate deviation.
3. From the model view, the top nav strip links to **accuracy** (performance grading + missed-majors table), **history** (predictions vs. actuals per day), and **pre-flare patterns** (aggregate biometric trajectories before flares).

### Evaluating a Medication or Intervention

1. Navigate to "Reactions" from the main menu (or `/interventions`).
2. If no cards appear, flag a medication as primary or secondary intervention first: go to `/clinical#medications`, edit the medication, tick the intervention checkbox.
3. Each intervention gets a card with pre/post flare stats, autonomic shift, and (for one-time doses) duration-of-effect.
4. Click **+ log event** on any card to record a side effect (with severity 0-10), rebound, efficacy change, dose change, or general note — these events are timestamped and filterable.

### Tuning the Model (Advanced)

1. Go to forecast page and click the green `>>` button (bottom-right) — or visit `/forecast/lab` directly. The same easter-egg `>>` also exists on the model dashboard.
   - Or search for "lab", "help", or "manual"
2. Type `?` for the user manual
3. Type `2` to adjust weights — all sliders exposed: symptom weights, category multipliers (UV, exertion, temperature, pain/fatigue), multi-day predictors (symptom burden, RMSSD deviation, RMSSD instability, respiratory rate deviation), and the flare threshold
4. Move sliders to customize weights
5. Click "Run Simulation" to preview changes against the last 60 days
6. Review accuracy/recall/precision impact and which predictions would flip
7. Click "Apply These Changes" to save (or "Reset to Defaults" to revert)

### Managing Clinical Records

1. Navigate to "Clinical" from the main menu
2. Add lab results, medications, events, or clinician contacts
3. Use the tabs to switch between record types
4. Click "Export" to download CSV files for specific date ranges
5. Edit or delete records using the action buttons

### Searching Your Data

1. Use the search bar in search tab
2. Type any keyword to search across all entries
3. Results are grouped by type (daily, labs, events, medications)
4. Click any result to view full details

### Remote Access (Optional)

See `REMOTE_ACCESS.md` for detailed instructions on setting up remote access via Raspberry Pi + Tailscale.

---

## Push Notifications via ntfy (Optional)

Biotracking uses [ntfy](https://ntfy.sh) for two kinds of phone notifications:

1. **Medication dose reminders** — fires at the scheduled time for each dose in your taper
2. **Proactive flare risk alerts** — fires once daily (default 8am) when your weighted risk score crosses the moderate threshold (≥ 5.0) or when you're about to enter a PMS/luteal phase. The alert includes your score, top contributing factors, and current cycle phase if relevant. High-risk alerts (≥ 8.0) use higher priority and a different tag so they stand out.

ntfy is a dead-simple open-source notification service. No account required. The Pi sends an HTTP POST; your phone receives a push notification. That's it.

### Set up ntfy

1. Install the **ntfy** app on your phone (App Store or Google Play, free, by Philipp Heckel)
2. Open the app and tap **Subscribe**
3. Enter a topic name — make it long and unguessable, like `biotracking-k7x9qm3p`
   (ntfy topics are public: anyone who knows the name can read and send to it, so don't use your name, your pet's name, or anything else obvious)
4. Add two keys to your `config.json` on the machine running biotracking:

```json
"ntfy_topic": "biotracking-k7x9qm3p",
"ntfy_server": "https://ntfy.sh"
```

1. Test it from your terminal before trusting your medication schedule to it:

```bash
curl -d "test notification" https://ntfy.sh/biotracking-k7x9qm3p
```

Your phone should buzz within a few seconds. If it doesn't, check that the topic name matches exactly and that notifications are enabled for the ntfy app in your phone's settings.

### Use the taper wizard

1. Go to **Clinical** → **Medications** tab
2. Add your steroid (e.g. methylprednisolone 4mg)
3. Click **set reminders** on that medication's row
4. The wizard pre-fills a standard 6-day Medrol dose pack schedule (18 doses, tapering from 6 tablets on day 1 to 1 tablet on day 6). Adjust the start date, times, and amounts as needed.
5. Click **activate reminders** — doses are saved and notifications will fire at the scheduled times as long as the app is running
6. Today's pending doses appear in a checklist on the **Daily Entry** page. Mark them taken as you go.

### Flare alert timing

The flare risk alert fires at 8am by default. To change the hour, add this to your `config.json`:

```json
"flare_alert_hour": 7
```

Then restart the app. The scheduler will pick it up. The alert rate-limits itself to once per calendar day — if risk drops below threshold later in the day, no second alert fires. If it fires and you want to reset it manually (e.g. to test), delete `config/flare_alert_state.json` and restart.

To disable flare alerts entirely without removing ntfy, just don't add `flare_alert_hour` — it won't suppress them. Instead, you can set `ntfy_topic` to an empty string in `config.json`, which disables all notifications.

### Notes

- Notifications only fire if the app is running. If you're on the Raspberry Pi setup described in `REMOTE_ACCESS.md`, the service runs continuously and this works reliably.
- Running locally on a Mac that sleeps? The scheduler pauses when the machine sleeps and resumes when it wakes. You may miss a dose notification or morning alert if the lid was closed.
- ntfy.sh is a public service run by one person. For higher reliability or privacy, you can self-host ntfy — change `ntfy_server` in `config.json` to your self-hosted URL.
- The taper wizard defaults to a standard Medrol 4mg dose pack. For other tapers, adjust the times and quantities in place or clear them and enter your own schedule.

---

## Optional Passcode (Access Control)

Health data can be sensitive in ways that go beyond the abstract. If you share a living space, use your laptop in shared areas, or are in any situation where you need your data visible only to you, the optional passcode adds a simple lock screen to the app.

**To enable:**

Open `config.json` (in your biotracking folder) in any text editor and add one line:

```json
"passcode": "yourpin"
```

For example, if your config currently ends with:

```json
  "debug": false,
  "secret_key": "abc123..."
}
```

Make it:

```json
  "debug": false,
  "secret_key": "abc123...",
  "passcode": "yourpin"
}
```

Restart the app. From now on, anyone visiting the app URL will see a passcode prompt before they can access any data.

A **lock** button will appear in the navigation bar. Clicking it ends your session immediately.

**To disable:** remove the `"passcode"` line from `config.json` and restart.

**Notes:**

- The passcode can be any string — a word, a number, a phrase. It's stored in your local `config.json` file, which is already gitignored and never committed to GitHub.
- This is a "lock the door" measure, not a cryptographic security system. It protects against casual access (someone picking up your laptop, a roommate, a family member) on a trusted home network. It is not a substitute for full-disk encryption if your threat model involves physical device seizure.
- If you forget your passcode, open `config.json` in a text editor and either read it there or remove the line.
- Sessions expire when you close the browser tab or click **lock**. There is no persistent "remember me."

---

## Troubleshooting

**"Port 5000 is already in use"** (common on macOS which uses 5000 for AirPlay)

Edit `app.py` and change `port=5000` to `port=5001`, then visit `http://localhost:5001`.

**UV data shows all zeros**

Check your longitude sign. North America longitudes should be negative (e.g., Oklahoma City: `35.4676, -97.5164`). Edit `config.json` and run `python backfill_uv.py --force`.

**Can't access from phone**

Make sure phone and computer are on the same WiFi. Verify the app is running. Try `http://` not `https://`. Check there's no firewall blocking port 5000.

**"No module named 'pandas'"**

You're not in the virtual environment. Run `source .venv/bin/activate` (Mac/Linux) or `.venv\Scripts\activate` (Windows) first.

---

## Data Management

**Your data lives in two files:**

- `biotracking.db` — the SQLite database (kept at this filename deliberately; renaming it to `sardine-track.db` would break existing installs that have the file in place, and the name inside the SQLite file is invisible to users anyway. The .gitignore correctly excludes this file regardless.)
- `config.json` — your settings and API keys

**Back them up:**

```bash
cp biotracking.db biotracking_backup_$(date +%Y%m%d).db
```

**Export options:**

- In-app: export buttons for labs, medications, events, clinician list (CSV)
- In-app UI delete function on search page
- DB Browser for SQLite (GUI tool, free)
- Command line: `sqlite3 biotracking.db .dump > backup.sql`

**Reset everything:**

```bash
rm biotracking.db config.json
python setup.py
```

This deletes all your data. Back up first.

---

## How the Flare Prediction Model Works

The flare prediction model is a transparent, statistical approach. No black box — you can see exactly how every prediction is made, and tune it yourself.

Each day receives a risk score (0-25) based on UV dose (same-day and 4-day cumulative with flattened decay weights), physical overexertion, temperature elevation, individual symptoms with laddered pain/fatigue contributions, and four multi-day predictors:

- **Symptom burden delta** — how many more symptom categories are active than your personal 14-day baseline. Flares build; they don't appear from nowhere. Originally the model's strongest single predictor.
- **RMSSD baseline deviation** — 7-day rolling vagal tone vs 30-day baseline. A sustained drop in parasympathetic activity (measured via Apple Watch RR-interval data) precedes inflammatory flares. Mechanistically grounded in the cholinergic anti-inflammatory pathway; empirically replicates Thanou 2016's ΔRMSSD-ΔSLEDAI finding.
- **RMSSD instability** — mean day-to-day |ΔRMSSD| over prior 5 days vs 30-day baseline. Captures autonomic *chaos* before major flares — RMSSD oscillates wildly (surge/crash/surge/collapse) rather than simply drifting down. Independent signal from the level-based deviation; both can fire together.
- **Respiratory rate baseline deviation** — 3-day rolling rate vs 14-day baseline. ICU-literature-motivated (Barfod 2017); validation on personal data is ongoing via the dashboard chart.

All four multi-day predictors use baseline-relative scoring rather than raw values, because chronic daily symptoms become constant offsets that don't distinguish flare days from non-flare days.

**Threshold**: score ≥ 8.0 = flare risk (default; tunable). All weights are tunable in the Forecast Lab. Major flare recall is tracked as the primary performance metric since function-limiting flares are the ones that matter most to catch.

For full details on every scoring category, the math behind multi-day context injection, severity-specific trajectory analysis, and relevant literature (Thanou 2016, Poliwczak 2017, Barfod 2017, Huston & Tracey 2011), see **[MODEL.md](MODEL.md)** (rendered in-app at `/model/docs`).

## For Developers

### Contributing

This project welcomes contributions, especially from people with lived experience of diagnostic complexity. Whether as patients, clinicians, loved ones, or those for whom this is their special interest.

Areas where help is needed:

- Additional data import formats (Fitbit, Garmin, etc.)
- More correlation analysis methods
- PDF export improvements
- Accessibility improvements
- Documentation and tutorials
- Translations
- New designs to include other evolving hard-to-diagnose disease that isn't my flavor of lupus.

Please open an issue before starting work on a major feature.

Also reach out to me at <alaric.moore@pm.me>

### Project Structure

```
sardine-track/
├── app.py                      # Flask routes, scoring model, forecast lab, migrations hook
├── db.py                       # All database operations; idempotent run_migrations() at startup
├── uv_fetcher.py               # UV API integration (Open-Meteo + Visual Crossing)
├── setup.py                    # First-run DB schema and per-user config
├── create_user.py              # CLI for adding additional users post-setup
├── MODEL.md                    # Full flare prediction model documentation (rendered at /model/docs)
├── CHANGELOG.md                # Dated list of substantive changes
├── CONTRIBUTING.md             # Contributor guidelines
├── COMMERCIAL_LICENSE.md       # Commercial licensing terms (AGPL-3.0 for non-commercial)
├── REMOTE_ACCESS.md            # Raspberry Pi + Tailscale + Oracle Cloud setup guide
├── help.md                     # In-app help text
├── requirements.txt            # Python dependencies
├── config.json                 # User settings & API keys (gitignored)
├── biotracking.db              # SQLite database (gitignored)
├── import_apple_health.py      # Apple Health CSV importer (HRV, sleep, wrist temp, daylight)
├── import_cycle.py             # Menstrual cycle Apple Health importer
├── import_labs.py              # Lab results CSV importer with ref-range auto-detection
├── import_tracker.py           # Generic symptom-tracker spreadsheet importer
├── import_backup.py            # Import data from a prior biotracking.db backup file
├── backfill_uv.py              # Historical UV data fetcher (Visual Crossing API)
├── migrate_symptoms.py         # One-off migration: symptom category reorganization
├── migrate_to_multiuser.py     # One-off migration: single-user → multi-user schema
├── rmssd_flare_rerun.py        # Standalone RMSSD pre-flare pattern analysis (generates PNG)
├── config/
│   ├── custom_weights.json     # Forecast Lab overrides (gitignored; per-user fallback)
│   └── flare_alert_state.json  # Daily alert rate-limit state (gitignored)
├── backups/                    # Local DB backup snapshots (gitignored)
└── templates/
    ├── base.html               # Shared layout + global CSS palette (colors referenced app-wide)
    ├── login.html, register.html
    ├── daily_entry.html, daily_confirm.html
    ├── mobile_base.html, mobile_log.html, mobile_status.html  # Phone-optimized entry flow
    ├── forecast.html           # Daily flare forecast with easter-egg >> link to lab
    ├── timeline.html           # Model dashboard (score attribution) — served at /model
    ├── forecast_lab.html       # Weight tuning interface
    ├── forecast_history.html   # Predictions vs actuals, ranked by score gap
    ├── forecast_accuracy.html  # Major/minor recall, missed-majors table, factor signal quality
    ├── forecast_patterns.html  # Pre-flare pattern analysis + RMSSD trajectories
    ├── interventions.html      # Per-medication pre/post evaluation + side-effects log
    ├── hrv.html                # Legacy autonomic view (still on disk, no longer nav-linked)
    ├── cycle.html              # Menstrual cycle calendar
    ├── uv_lag.html             # UV-symptom correlation at 0/1/2/3/4-day lags
    ├── wearable.html           # UV wearable view (prototype) — per-sample UV chart + daily dose
    ├── clinical_record.html    # Labs, medications, events, clinicians, ANA
    ├── settings.html, admin.html
    ├── report.html
    ├── search.html
    ├── readme.html             # Renders README.md in-app
    └── remote_access.html      # Renders REMOTE_ACCESS.md in-app
```

The iOS companion (sardinessync) lives in its own repo at [github.com/alaricmoore/sardinessync](https://github.com/alaricmoore/sardinessync). It used to live in this repo at `ios-health-sync/` but was extracted so the iOS and Flask codebases could evolve independently.

---

## License

GNU Affero General Public License v3.0 (AGPL-3.0)

This software is free for individuals and non-profits with attribution. Commercial entities wishing to use, modify, or deploy this software must obtain a separate commercial license.

The AGPL-3.0 requires that if you modify and deploy this software (including as a web service), you must make your modified source code available under the same license.

See the [LICENSE](LICENSE) file for full terms. For commercial licensing inquiries, contact the author.

---

## Support

For bugs, feature requests, or questions, open an issue on GitHub. Check existing issues first -- your question might already be answered.

This is currently a one-person project built between doctor appointments and fixing machines and building terrariums. Response times may vary.

Take care of yourself. Trust your observations. Keep asking questions.
