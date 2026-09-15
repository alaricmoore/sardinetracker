# Importing your data — sardinetracker

Already tracking somewhere else? These scripts bring it in. Each one takes `--dry-run`, so you can see what it would do before it writes anything. Run them from the sardinetracker folder with your virtual environment active (see the [README](README.md)).

## From Apple Health

Sardinetracker can import HRV, sleep hours, wrist temperature, and daylight exposure from Apple Health, or whatever else you are tracking. Which provides free-of-cost raw data downloads.

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

## From Apple Health — Menstrual Cycle Data

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

## From Your Own Symptom Tracker

If you've been tracking in a spreadsheet, you can import it. Required column: `Date` (in YYYY-MM-DD, MM/DD/YYYY, or "Jul 22, 2025" format). Optional columns are mapped automatically for symptom flags, pain/fatigue scales, sleep hours, and notes.

```bash
python import_tracker.py path/to/your_tracker.csv --dry-run
python import_tracker.py path/to/your_tracker.csv
```

## Lab Results

```csv
Date,Test,Value,Units,Lab,Doctor
2021-04-16,C4,28,mg/dL,LabCorp,Dr. Smith
```

```bash
python import_labs.py path/to/labs.csv --dry-run
python import_labs.py path/to/labs.csv
```

The script auto-detects reference ranges and flags common tests (C3, C4, CRP, ESR, anti-dsDNA, etc.).

## UV Data Backfill

After importing historical data, fetch UV values for those dates:

```bash
python backfill_uv.py
```

You'll need a free [Visual Crossing](https://visualcrossing.com) API key. Add it to `config.json`:

```json
"visual_crossing_key": "YOUR_KEY_HERE"
```

> The free tier allows 1000 records/day. Historical UV uses ~24 records per day, so you can backfill about 40 days for free. Beyond that, the metered plan is $0.0001/record.
