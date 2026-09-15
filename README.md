# sardinetracker (biotracking)

A local-only health tracking application for patients navigating complex diagnostic journeys.

Built for people who need to see patterns in their own data when the medical system isn't connecting the dots yet.

(Well, built for one person who needed to see patterns, but she figured she couldn't be the only nut out there.)

> **Note:** This is the public codebase of what was originally released as `biotracking`, briefly `sardine-track`, and now named **sardinetracker** (a SARDs pun — "systemic autoimmune rheumatic disease," plus a tracker that sticks with you — matching [sardinetracker.com](https://sardinetracker.com)). The GitHub renames keep the old URLs as redirects, so existing links still work. Active experimentation continues in a private fork that may push features back upstream when they prove out. The phone companions live in their own repos: **[sardinessync](https://github.com/alaricmoore/sardinessync)** (iOS/Apple Health) and **[sardinesync-android](https://github.com/alaricmoore/sardinesync-android)** (Android/Health Connect — for anyone whose wearable isn't an Apple Watch). Want to know how your data is stored and how it gets to your phone? See the [remote access guide](REMOTE_ACCESS.md).
>
> **About the family-instance framing:** the app supports multiple users and was originally built hoping family with shared genetic risk might want to track alongside. In practice the daily-entry burden has kept adoption to one. Multi-user plumbing is preserved — if a family member or friend does decide to try, they can register their own account on the same instance without affecting anyone else's data.

---

## What It Does

Sardinetracker helps you:

- Track daily symptoms, biometrics, and environmental factors (including UV exposure, in fact especially UV exposure)
- Visualize correlations over time (does UV exposure predict your symptom flares? does low HRV precede bad days?)
- Generate clinical reports to bring to appointments (when you know damn well your brain is not going to remember everything, plus it has graphs!)
- Keep a longitudinal record of labs, medications, & clinical events, as well a list of your clinicians
- Run flare forecasting based on your own historical patterns — transparent scoring, not a black box, and tuned on your own n=1 data
- Evaluate medical interventions (hydroxychloroquine, steroids, biologics, whatever): per-medication pre/post flare impact, autonomic shift, duration-of-effect for one-time doses, and structured logging of side effects, rebounds, and dose changes
- Auto-sync biometrics from your phone: the **[sardinessync](https://github.com/alaricmoore/sardinessync)** iOS companion (Apple Health) or the **[sardinesync-android](https://github.com/alaricmoore/sardinesync-android)** Android companion (Health Connect — works with any wearable that writes to it: Fitbit, Garmin, Samsung, Oura, Pixel Watch, not just an Apple Watch). Steps, HRV/SDNN, RMSSD, resting heart rate, SpO2, respiratory rate, basal body temperature, time in daylight.
- Share a read-only, expiring view of your record with a clinician — one link, no account needed on their end, revocable any time
- Keep all your data local — nothing leaves your computer, if you don't want it to.

Every feature, section by section, is in **[FEATURES.md](FEATURES.md)**.

This is not a medical product. This is a tool for veracity: for people who need to make their invisible patterns visible, whether for themselves or to make a case to others.

---

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
- It runs on your own machine. One instance can hold more than one account, and each account's records are kept separate.
- Do not use this application to track anyone's health data without their informed consent. Don't be creepy.

---

## Requirements

- macOS, Linux, or Windows (tested primarily on macOS and Linux... actually not tested on Windows. Sorry.)
- Python 3.9 or later (earlier veersions work, but watch your D's and d's)
- A web browser (Brave, Firefox, Safari, Edge, Opera, Tor...)
- Optional: an iPhone with Apple Health, or an Android phone with Health Connect, for biometric sync (I have an apple watch, because access to raw data for free and it's also a watch)

---

## Installation

### Step 1: Install Python

**macOS/Linux:** Python 3 is likely already installed. Open Terminal and check:

```bash
python3 --version
```

If you see Python 3.9 or higher, you're good. If not, download from [python.org](https://python.org).

**Windows:** Download Python from [python.org](https://python.org) and make sure to check "Add Python to PATH" during installation.

### Step 2: Download sardinetracker

**Option A: Download ZIP (easiest if you're not familiar with git)**

1. Go to the GitHub repository page
2. Click the green **Code** button
3. Click **Download ZIP**
4. Unzip the file to a folder you can find (like `Documents/sardinetracker`)

**Option B: Clone with git**

```bash
git clone https://github.com/alaricmoore/sardinetracker.git
cd sardinetracker
```

> The repo was formerly named `biotracking`, then `sardine-track`; the old URLs still redirect. If you want the iOS companion as well, the Swift sources live in a separate repo: [github.com/alaricmoore/sardinessync](https://github.com/alaricmoore/sardinessync).

### Step 3: Set Up the Application

Open Terminal (Mac/Linux) or Command Prompt (Windows), navigate to the sardinetracker folder, and run:

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

# Create your login (asks for a username and password)
python create_user.py --admin
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
sardinetracker
==============
Patient: Your Name
Starting server...

Local:  http://localhost:5000
Phone:  connect to same wifi, visit http://<your-ip>:5000
```

> **A note on the name:** internally, the code still calls itself `biotracking` in a lot of places (module docstrings, the `biotracking.db` filename, some comments). That was the project's original name before it became `sardinetracker`. It's left alone on purpose — renaming every occurrence is churn without benefit, and the database file in particular would break existing installs if renamed. User-facing surfaces (this banner, script output, `--help` text) say `sardinetracker`.

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

---

## Importing Your Data

Already have data in Apple Health, a spreadsheet, or a pile of lab results? **[IMPORTING.md](IMPORTING.md)** walks through each importer, plus backfilling historical UV.

---

## Using It

The **[help guide](help.md)** covers daily logging, the forecast and the Forecast Lab, interventions, search, notifications and backups. The same guide is in the app under **help**.

---

## Accounts and Login

Every page sits behind a username and password. Passwords are stored as bcrypt hashes, never as plain text.

Health data can be sensitive in ways that go beyond the abstract. If you share a living space, use your laptop in shared areas, or need your data visible only to you, your login is the lock.

**Create accounts** on the machine running sardinetracker:

```bash
python create_user.py --admin    # your own account, with admin rights
python create_user.py            # another account
python create_user.py --list     # see who has an account
```

**Let someone register themselves.** Add an invite code to `config.json` and restart:

```json
"registration_invite_code": "a-long-phrase-only-they-know"
```

`/register` then accepts that code. Once they've signed up, remove the line and restart, so nobody else can use it.

**"Remember me" lasts a year** on that browser. On a borrowed or shared device, leave it unticked and log out when you're done.

**Exposing the app to the internet?** A login alone isn't enough; the form doesn't limit guesses. Read the "Harden the app itself" section of [REMOTE_ACCESS.md](REMOTE_ACCESS.md) first.

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

Contributions are welcome, especially from people with lived experience of diagnostic complexity, whether as patients, clinicians, loved ones, or those for whom this is their special interest. **[CONTRIBUTING.md](CONTRIBUTING.md)** covers how to propose a change; **[DEVELOPING.md](DEVELOPING.md)** maps the code, the tests and the project structure.

Also reach out to me at <alaric.moore@pm.me>

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
