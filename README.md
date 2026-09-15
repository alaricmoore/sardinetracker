# sardinetracker (biotracking)

A local-only health tracking application for patients navigating complex diagnostic journeys.

Built for people who need to see patterns in their own data when the medical system isn't connecting the dots yet.

(Well, built for one person who needed to see patterns, but she figured she couldn't be the only nut out there.)

The name is a pun on SARDs, systemic autoimmune rheumatic diseases, and it was originally released as `biotracking` (old GitHub links still redirect). It supports more than one account, so family or friends can track on the same instance, each with their own records. Active experimentation happens in a private fork, and features come back here once they prove out.

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

> You'll still see the old name `biotracking` inside the code and in the database filename, `biotracking.db`. That's deliberate: renaming the file would break existing installs.

Open your browser and go to `http://localhost:5000`. Try adding today's entry to make sure everything works. If something's off, see [first-run troubleshooting](TROUBLESHOOTING.md#first-run-on-your-own-computer).

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

## Where to Next

| Doc | What's in it |
|---|---|
| **[help.md](help.md)** | Using the app day to day: what to log, the forecast and Forecast Lab, interventions, notifications, backups. Also in the app under **help**. |
| **[FEATURES.md](FEATURES.md)** | Everything it does, one area at a time |
| **[IMPORTING.md](IMPORTING.md)** | Bringing in Apple Health exports, cycle data, a spreadsheet tracker, lab results, and historical UV |
| **[MODEL.md](MODEL.md)** | How the flare score is calculated, every category and the research behind it (also in the app at `/model/docs`) |
| **[REMOTE_ACCESS.md](REMOTE_ACCESS.md)** | Reaching your instance from outside the house, and hardening it before you do |
| **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** | First-run problems (port in use, can't log in, UV all zeros), then triage for a deployed instance |
| **[WHY.md](WHY.md)** | The story behind the project |
| **[DEVELOPING.md](DEVELOPING.md)** | The code map, tests and project structure, for people changing the code |

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
