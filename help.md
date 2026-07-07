# Sardine-track — Help Guide

*A plain-English guide to using the app and understanding what it's tracking.*

---

## What Is This App?

Sardine-track (formerly biotracker) is a personal health logging app built specifically for people managing photosensitive autoimmune and rheumatic conditions — things like lupus, mixed connective tissue disease, or any illness where symptoms are unpredictable, hard to explain to doctors, and deeply affected by things like sun exposure, sleep, and hormonal cycles.

It was built by Alaric, who has lupus, because she couldn't find a tool that did what she actually needed: connect the dots between daily life and disease activity, and produce something useful to bring to a 15-minute doctor's appointment.

Your data lives on whichever server you're logged into — if you're reading this on someone else's instance, ask them where it's hosted and how it's backed up. If you're running your own, it's in a SQLite file on your own machine. Either way it is not sold, shared, or sent to any third party. UV index data is pulled from a public weather API anonymously using only GPS coordinates — that's the only outside connection the app makes.

---

## What Should I Log Every Day?

The daily entry form is the core of the app. You don't have to fill out every field — log what feels relevant. The more consistently you log, even minimally, the more useful the patterns become over time.

**The fields the prediction model leans on most:**
- **Symptoms** — which categories are active (neurological, cognitive, musculature, migraine, pulmonary, dermatological, mucosal, rheumatic, gastro). For rheumatic, mentioning a specific joint in the notes ("right knee", "jaw") lets the model weight it higher than unspecified aches.
- **Pain and fatigue** on the 1-10 scale — the model scores these at every step from 4 upward, not just at 7+. A pain-6 day or a fatigue-5 day counts.
- **Flare occurred?** — yes or no, plus severity (minor vs. major). This is the ground truth the model is trying to predict.
- **Sun exposure minutes** and **UV protection level** — how long outside, and whether you wore SPF/hat/full cover/stayed indoors. The model multiplies UV index by exposure time by a protection factor.
- **Sleep hours** — short sleep drives up the overexertion score.

Biometrics from your Apple Watch (HRV, RMSSD, resting heart rate, respiratory rate, basal body temperature) fill in automatically if you're using the sardinessync iOS app or the Apple Health Shortcut — you don't have to type those.

Everything else adds context and richness over time but won't change your daily score dramatically.

**On hard days**, there's a simplified quick-entry mode that shows only the fields the model needs. You can reach it by adding `?mode=quick` to the end of the daily entry URL, or tap "quick entry" in the daily form.

---

## What the Numbers Mean

### Pain Scale (1–10)
- **1–3**: Mild, noticeable but not limiting
- **4–6**: Moderate, affecting what you can do
- **7–9**: Severe, significantly limiting
- **10**: Worst pain imaginable / emergency territory

### Fatigue Scale (1–10)
Same idea. A 7 fatigue day is "I cannot get up even though I want to." A 3 is "tired but functional."

### UV Index
This is a measure of how strong the ultraviolet radiation from the sun is on a given day, on a scale from 0 to 11+.

| UV Index | Level |
|----------|-------|
| 0–2 | Low |
| 3–5 | Moderate |
| 6–7 | High |
| 8–10 | Very High |
| 11+ | Extreme |

For photosensitive conditions, UV exposure on one day can trigger a flare one, two, or even three days later — not necessarily the same day. The app tracks this lag and shows you your personal pattern.

---

## What Is HRV?

**HRV stands for Heart Rate Variability.** It sounds technical but the concept is simple.

Your heart doesn't beat like a metronome. Even at rest, the time between beats varies slightly — sometimes a bit longer, sometimes a bit shorter. That variation is HRV.

**Higher HRV** generally means your nervous system is relaxed, recovered, and adaptable. Your body has resources to work with.

**Lower HRV** generally means your body is under stress — from illness, poor sleep, inflammation, or overexertion.

For people with autoimmune conditions, HRV can drop noticeably in the day or two *before* a flare becomes obvious. It's your body signaling that something is off before you fully feel it.

You can track HRV with a wearable like an Apple Watch, Garmin, Fitbit, or Whoop. The app lets you log your morning HRV reading and visualize how it trends alongside your symptoms over time.

You don't have to track HRV to use the app — it's an optional field. But if you have a device that measures it, it's one of the more interesting things to correlate with flare activity.

---

## What Is Basal Body Temperature (BBT)?

**Basal body temperature is your body temperature at complete rest** — typically measured first thing in the morning before getting up, eating, or doing anything. It's usually taken with a special BBT thermometer that reads to two decimal places (like 97.42°F rather than just 97.4°F), though a regular digital thermometer works too.

### Why does it matter for autoimmune disease?

BBT does two useful things in this context:

**1. It can signal inflammation.** When your immune system is active — fighting something, reacting to a trigger — your resting temperature can shift subtly before you feel obviously sick. A pattern of slightly elevated morning temperatures can be an early signal.

**2. It tracks your menstrual cycle phase.** This matters because hormonal cycles interact with immune activity. After ovulation, progesterone causes BBT to rise by about 0.2–0.5°F and stay elevated until your next period. The app detects this shift to figure out which phase of your cycle you're in — which is much more accurate than just counting days, especially if steroids or disease activity make your cycle irregular.

### How to log it

Take your temperature first thing in the morning, before getting out of bed, and log it in the daily entry. Even just a few months of data starts to reveal patterns.

---

## The Forecast Model

After about 7 days of logging, the app starts generating a daily flare risk score. This is not a medical prediction — it's a statistical pattern based on *your own* historical data.

The score comes from adding up contributions from a dozen or so categories. You can see exactly what fired and how many points each contributed on the **Forecast** page, and the full breakdown over time on the **Model** page (`/model` in the URL).

**What the model looks at:**

- **UV dose today** — weighted UV index times minutes outside times a protection factor. Being in the sun at UV 9 for an hour without protection scores much higher than 30 min at UV 3.
- **UV over the prior 4 days** — sun damage accumulates. The model adds up recent days with decay weights so yesterday counts more than 4 days ago, but 4 days ago still counts.
- **Overexertion** — steps relative to your personal baseline, adjusted for sleep. A big walk on 4 hours of sleep scores higher than the same walk after 8.
- **Basal body temperature elevation** — subtle rises above your personal baseline may signal building inflammation.
- **Active symptoms** — each category contributes its own points, with rheumatic (joint) pain scored higher if you name a specific joint vs. an unspecified ache.
- **Pain and fatigue** — *laddered* scoring. A pain of 4 counts a little, 5 counts more, 6 more, 7 more again. This replaces an older "only counts at 7+" threshold that missed function-limiting days where a single high-severity symptom was the whole story.
- **Symptom burden delta** — how many more symptom categories are active than your personal 14-day baseline. Captures *acceleration* rather than raw count, which matters because chronic daily symptoms become constant noise and don't distinguish flare days.
- **RMSSD deviation** — if your 7-day rolling RMSSD (a vagal-tone measure) is sitting noticeably below your 30-day baseline, that's "vagal withdrawal." Empirically precedes flares; mechanistically grounded in the cholinergic anti-inflammatory pathway.
- **RMSSD instability** — separate signal from the above. Measures how much your RMSSD has been *oscillating* day-to-day. Autonomic chaos (wild swings) tends to precede major flares more reliably than a simple steady drop. An additive independent signal — both the deviation and instability can fire on the same day.
- **Respiratory rate deviation** — if your 3-day rolling respiratory rate is climbing above your 14-day baseline, the model scores it. This is a pending-validation feature based on ICU literature; the dashboard chart shows whether it's actually earning its weight in your own data.
- **Emotional state** — a low day (mood ≤ 4) adds points, because emotional state often correlates with inflammatory load.
- **Cycle phase** — if cycle tracking is enabled, luteal / PMS windows can add points. Currently weight 0 in defaults because the signal wasn't predictive in the author's data, but the hook is there if yours is different.

It adds everything up into a risk score from 0 to 25. A threshold (default 8.0, tunable per user) divides "probably just a regular day" from "flare likely." All weights are adjustable in the **Forecast Lab**.

**The more data you have, the more accurate it becomes.** Early on it will be rough. Over months, with 20+ logged flares under your belt, the within-person patterns get meaningful and the model can actually call it.

The Model page shows you, visually, which days hit high risk and whether a flare actually followed — so you can see where the model is doing well and where it's missing.

---

## The Clinical Record

This section is for organizing the medical side of your life — not just symptoms.

- **Labs**: Log test results with values, reference ranges, and dates
- **Medications**: Track what you've taken and when, with start and end dates
- **Clinical Events**: Appointments, ER visits, procedures — with notes
- **Clinicians**: A directory of your care team
- **ANA Tracking**: Specialized tracking for ANA titers and patterns (relevant for lupus diagnosis)

You can export any of these as a CSV file, which is useful for sending to a new provider, requesting records, or just keeping a personal copy.

---

## The Interventions View (`/interventions`)

This is where you answer the question **"did that medication actually help?"** in stats rather than vibes.

For each medication you've marked as a primary or secondary intervention (the tickbox on the medication edit form in `/clinical`), the Interventions page shows you a card with:

- **Flare impact** — total flare count before vs. after starting it, broken down by severity (major, minor, ER), plus the average number of days between flares. Delta percentages are color-coded: green for improvement, red for worsening.
- **Autonomic shift** — average RMSSD, SDNN, and respiratory rate before vs. after, so you can see whether the medication calmed or riled your nervous system.
- **Duration of effect** (for one-time doses like a dex IV or a steroid injection) — how many days until the next flare of each severity, and how long it took your autonomic metrics to settle back to their pre-dose baseline.
- **Rebound flag** (also for one-time doses) — if a medication helped initially but flares surged back 2-6 weeks later, an amber banner will call it out so you don't have to eyeball it yourself.

### Logging side effects, rebounds, and dose changes

Every intervention card has an "events" section where you can log dated observations about that specific medication:

- **Side effect** — with a severity slider 0-10. Example: "2026-02-14, HCQ, severity 3, mild GI upset if taken on empty stomach."
- **Rebound** — manually confirming a rebound pattern, or noting an unexpected flare return.
- **Efficacy change** — "seems to be helping less lately" or "big improvement this month."
- **Dose change** — timestamped dose adjustments separate from editing the medication record itself (so you can see "200mg → 400mg on 2026-02-14" as an event in context).
- **Note** — general observation, no severity field.

### Why a separate view instead of putting all of this in symptoms

The Interventions view is for medication-attributed observations. Regular symptom logging is for "this happened today, cause unknown." Keeping them separate preserves the distinction between "my body did X" and "I think the pill I took yesterday caused X." When you're talking to a rheumatologist about whether to continue a drug, the attributed events are what they need.

---

## Notifications (ntfy)

The app can send you a daily reminder to log, and an alert if your flare risk is elevated. This uses a free service called ntfy — you install the ntfy app on your phone, subscribe to a private channel, and that's it. No account required.

Setup instructions are in your account profile.

---

## Auto-Sync from Apple Health

If you have an Apple Watch, you can have your iPhone send biometric data to sardinetracker automatically — no typing required for steps, HRV, resting heart rate, respiratory rate, SpO2, or basal body temperature.

There are two ways to set this up. The **native iOS app** is more capable but requires you to build it yourself in Xcode. The **iOS Shortcut** approach is simpler but limited.

### Option A: sardinessync iOS app (recommended if you have a Mac)

**[sardinessync](https://github.com/alaricmoore/sardinessync)** is a native iOS companion app that:

- Reads everything HealthKit offers, including the things Shortcuts can't reach (RR intervals for overnight RMSSD, Time in Daylight, respiratory rate)
- Computes RMSSD from raw heartbeat intervals on your phone (better accuracy than Apple's built-in HRV number)
- Handles background sync automatically — no manual triggering
- Gives you a tab with mobile-friendly sardinetracker pages and local push notifications for flare alerts / medication doses

It's not in the App Store (not paying Apple $99/yr for a hobby project's listing). You clone the repo, open it in Xcode, plug your iPhone in, and build. With a free personal Apple ID the app expires every 7 days and needs a re-install — about 2 minutes if you leave Xcode configured. If you pay Apple the $99/yr, it lasts a year between rebuilds.

See the sardinessync repo's README for the full setup walkthrough. The short version: change the bundle ID to something unique to you, set signing team to your personal Apple ID, hit build, configure the server URL and API token inside the app on first launch.

### Option B: iOS Shortcut (no Xcode required)

This uses **iOS Shortcuts**, a built-in iPhone feature that lets you chain together small actions (like "read my step count" and "send it to a website") without writing any code.

It's a reasonable fallback if you can't or won't touch Xcode. Downsides vs. the native app:
- Can't compute RMSSD from RR intervals (Apple doesn't expose that data type to Shortcuts)
- Can't read Time in Daylight (sun exposure minutes)
- No background scheduling — has to be triggered by a Shortcuts automation or opened manually
- No local notifications tied to your data

### What gets synced (Shortcut version)

- **Steps** — your total for the day
- **HRV (SDNN)** — heart rate variability from your watch
- **Resting heart rate** — useful for tracking tachycardia or inflammation patterns
- **Basal body temperature** — the delta your watch calculates from your personal baseline

### What doesn't get synced via Shortcuts

- **Sleep** — Apple Health has trouble with polyphasic sleep and sleepwalking, so sleep is better entered manually
- **Sun exposure minutes** — Apple tracks "Time in Daylight" on the watch but doesn't make it available to Shortcuts (thanks, Apple). The sardinessync native app *can* read this.
- **RMSSD** — requires raw RR interval data, which Shortcuts can't access. Native app only.
- **Symptoms, flare status, notes** — these are personal observations that only you can provide

### How to set it up

1. Open the **Shortcuts** app on your iPhone (it's pre-installed — blue and pink icon)
2. Tap **+** to create a new shortcut, name it something like "Health Sync"
3. Use the search bar to add these actions in order:

**Get the date:**
- Add a **Date** action
- Add a **Format Date** action — set to Custom format: `yyyy-MM-dd`

**Pull your health data (add four "Find Health Samples" actions):**
- Step Count — sort by Start Date, Most Recent, limit 1
- Heart Rate Variability — sort by Start Date, Most Recent, limit 1
- Resting Heart Rate — sort by Start Date, Most Recent, limit 1
- Body Temperature — sort by Start Date, Most Recent, limit 1

For Steps, make sure you're getting the sum for the day, not just the last sample.

**Build the data package:**
- Add a **Dictionary** action with these keys:
  - `user_id` (Number) — your user ID, usually `1`
  - `date` (Text) — select the formatted date from earlier
  - `steps` (Number) — select the step count result
  - `hrv` (Number) — select the HRV result
  - `resting_heart_rate` (Number) — select the resting HR result
  - `basal_temp_delta` (Number) — select the body temperature result

**Send it:**
- Add **Get Contents of URL**
  - URL: your biotracker address followed by `/api/health-sync`
  - Method: POST
  - Add header `Authorization` with value `Bearer` followed by your API token (from config.json on the server)
  - Add header `Content-Type` with value `application/json`
  - Request Body: JSON — select the Dictionary

**Test it** by tapping the play button. You should see a response with `"ok": true`.

### Make it automatic

Go to the **Automation** tab in Shortcuts and set your shortcut to run automatically. Good trigger options:

- **Bedtime begins** — syncs when your wind-down starts
- **Time of Day** — set to late evening (like 11:50 PM)

Set it to **Run Immediately** so it doesn't ask for confirmation each time.

Once set up, your phone handles this in the background every night. On bad days — the days you need the data most — it's one less thing to do.

### A note about your API token

The token in your Shortcut gives write access to a limited set of biometric fields. It cannot touch your symptoms, medications, flare logs, or notes. But treat it like a password — don't share your Shortcut with anyone you wouldn't trust with your biotracker login.

---

## A Note on What This Is (and Isn't)

This app is not a medical device and does not give medical advice. It is a record-keeping and pattern-visualization tool.

What it does well:
- Helps you notice your own patterns
- Gives you something concrete to bring to appointments
- Creates a longitudinal record that would otherwise exist only as fragmented memories

What it can't do:
- Diagnose anything
- Replace your doctors
- Predict the future with certainty

Use it as evidence for conversations with your care team, not as a substitute for those conversations.

---

## Tips for Getting Started

**Log daily, even briefly.** Consistency matters more than completeness. A 30-second entry every day is worth more than a detailed entry once a week.

**Mark your flare days.** This is the most important thing for the prediction model. If you're having a bad day, check the flare box.

**Don't stress about missing days.** Life happens. A gap in the data is fine — the model works around it.

**Use the notes fields.** You don't have to write much, but "started new medication today" or "out in the sun for two hours" adds context that pure numbers can't capture.

**Come back to the Model page.** After a month or two of data, the Model dashboard (`/model`) starts showing you things. UV spikes followed by symptom spikes a day later. Sleep drops before flares. RMSSD wobbling before a bad week. Patterns you couldn't see in the day-to-day. Click any of the expandable trend charts to pop it out to full size.

**Check the Interventions page after a medication change.** Give it a few weeks of data after starting or stopping a medication, then look at the intervention card. If your flare rate drops 40% post-start, that's information worth taking to your rheumatologist. If it doesn't budge, that's also information.

---

*Built by C. Alaric Moore. Data stays on whatever server you're signed into — ask your host if you don't know.*

*If something isn't working or you want a feature added, just ask.*
