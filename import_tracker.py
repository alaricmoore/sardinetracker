"""
biotracking import_tracker.py
------------------------------
Imports historical symptom tracker CSV data into the biotracking database.

Maps old column names to the new schema, calculates basal temp delta
from config.json baseline, and preserves food/trigger notes in the
general notes field.

Derived/calculated columns are intentionally skipped:
    Primed Score, 3-Day Avg, PredictedFlare, Calculated Flare Prime Score

Usage:
    python import_tracker.py path/to/your_tracker.csv

    # Dry run - shows what would be imported without writing anything:
    python import_tracker.py path/to/your_tracker.csv --dry-run

    # Preview first 5 rows only:
    python import_tracker.py path/to/your_tracker.csv --preview 5

    # Import for a specific user:
    python import_tracker.py path/to/your_tracker.csv --user-id 2
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from typing import Optional

from numpy import record


# ============================================================
# Config
# ============================================================

def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        print("ERROR: config.json not found. Run setup.py first.")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


# ============================================================
# Column mapping
# Old tracker column name -> new schema field name
# ============================================================

COLUMN_MAP = {
    "Date":                                     "date",
    "Apple Health: Sun Exposure (min)":         "sun_exposure_min",
    "Apple Health: Step Count":                 "steps",
    "Apple Health: Basal Temp (F)":             "basal_temp_raw",   # already a delta in old tracker, so we can skip the baseline adjustment
    "Apple Health: HRV":                        "hrv",
    "Hours Slept":                              "hours_slept",
    "Manual: Neurological Symptoms (Y/N)":      "neurological",
    "Manual: Cognitive Symptoms (Y/N)":         "cognitive",
    "Manual: Musculature Symptoms (Y/N)":       "musculature",
    "Manual: Migraine or Headache (Y/N)":       "migraine",
    "Manual: Air Hunger/Chest Discomfort (Y/N)":"air_hunger",
    "Manual: Dermatological Stuff":             "dermatological",
    "Manual: Word Loss or Stuttering (Y/N)":    "word_loss",
    "Manual: Emotional State":                  "emotional_state",
    "Manual: Pain Scale (0-10)":                "pain_scale",
    "Manual: Fatigue Level (1-10)":             "fatigue_scale",
    "Manual: Other Notable Triggers (notes)":   "_triggers",        # folded into notes
    "Manual: Novel or persistent food cravings":"_cravings",        # folded into notes
    "Manual: Did you eat the thing?":           "_ate_it",          # folded into notes
    "Strike 1: Physical/Cognitive Load (Y/N)":  "strike_physical",
    "Strike 2: UV/Heat/Allergen/Other (Y/N)":   "strike_environmental",
    "Flare":                                    "flare_occurred",
    "Notes/Reflection":                         "notes",

    # Derived columns - explicitly skipped
    "Primed Score (Auto Calculated)":           "_skip",
    "3-Day Avg Primed Score %":                 "_skip",
    "Calculated Flare Prime Score: Today":      "_skip",
    "PredictedFlare":                           "_skip",
}


# ============================================================
# Value converters
# ============================================================

def parse_date(value: str) -> Optional[str]:
    """Parse date from various formats to YYYY-MM-DD."""
    if not value or not value.strip():
        return None
    value = value.strip()

    formats = [
        "%b %d, %Y",    # Jul 22, 2025
        "%B %d, %Y",    # July 22, 2025
        "%Y-%m-%d",     # 2025-07-22
        "%m/%d/%Y",     # 07/22/2025
        "%m/%d/%y",     # 07/22/25
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    print(f"  WARNING: could not parse date '{value}' — skipping row")
    return None


def parse_bool(value: str) -> int:
    """Convert Y/N or variants to 1/0.
    Treats any non-empty, non-N, non-no value as True.
    Handles warning symbols (⚠) as True.
    """
    if not value or not value.strip():
        return 0
    v = value.strip().upper()
    if v in ("N", "NO", "FALSE", "0", ""):
        return 0
    if v in ("Y", "YES", "TRUE", "1"):
        return 1
    # Non-standard but non-empty (warning symbol, free text) = True
    return 1


def parse_float(value: str) -> Optional[float]:
    """Parse a numeric string, return None if empty or unparseable."""
    if not value or not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def parse_int(value: str) -> Optional[int]:
    """Parse an integer string, return None if empty or unparseable."""
    f = parse_float(value)
    return int(f) if f is not None else None


def build_notes(row_mapped: dict, original_notes: str) -> Optional[str]:
    """Combine original notes with food cravings and trigger data.
    Preserves these fields as text rather than losing them.
    """
    parts = []

    if original_notes and original_notes.strip():
        parts.append(original_notes.strip())

    triggers = row_mapped.get("_triggers", "").strip()
    if triggers and triggers.upper() not in ("", "N", "NO", "NONE"):
        parts.append(f"[triggers: {triggers}]")

    cravings = row_mapped.get("_cravings", "").strip()
    ate_it = row_mapped.get("_ate_it", "").strip()
    if cravings and cravings.upper() not in ("", "N", "NO", "NONE"):
        if ate_it and ate_it.upper() not in ("", "N", "NO", "NONE"):
            parts.append(f"[cravings: {cravings} — ate: {ate_it}]")
        else:
            parts.append(f"[cravings: {cravings}]")

    return "\n".join(parts) if parts else None


# ============================================================
# Row processor
# ============================================================

def process_row(raw_row: dict, temp_baseline: float) -> Optional[dict]:
    """Map a raw CSV row to the daily_observations schema.

    Args:
        raw_row: dict of {column_name: value} from CSV
        temp_baseline: patient's baseline temp from config.json

    Returns:
        dict ready for db.upsert_daily_observation(), or None to skip
    """
    # First pass: rename columns using COLUMN_MAP
    mapped = {}
    for col, value in raw_row.items():
        col_clean = col.strip()
        target = COLUMN_MAP.get(col_clean)
        if target is None:
            # Unknown column - preserve in case it's useful
            mapped[f"_unknown_{col_clean}"] = value
        elif target != "_skip":
            mapped[target] = value

    # Parse date first - skip row if no valid date
    date_str = parse_date(mapped.get("date", ""))
    if not date_str:
        return None

    # Skip rows that are entirely empty (no symptom data at all)
    symptom_fields = [
        "neurological", "cognitive", "musculature", "migraine",
        "air_hunger", "dermatological", "word_loss"
    ]
    symptom_values = [mapped.get(f, "").strip() for f in symptom_fields]
    if all(v in ("", "N", "0") for v in symptom_values):
        # Check if there's any other data worth keeping
        has_biometrics = any([
            mapped.get("steps", "").strip(),
            mapped.get("hours_slept", "").strip(),
            mapped.get("hrv", "").strip(),
            mapped.get("pain_scale", "").strip(),
        ])
        if not has_biometrics and not mapped.get("notes", "").strip():
            return None  # completely empty row, skip

    # Build the output record
    record = {"date": date_str}

    # Biometrics
    record["steps"] = parse_int(mapped.get("steps", ""))
    record["hours_slept"] = parse_float(mapped.get("hours_slept", ""))
    record["hrv"] = parse_float(mapped.get("hrv", ""))
    record["sun_exposure_min"] = parse_int(mapped.get("sun_exposure_min", ""))

    
    # Basal temp: already stored as delta in old tracker
    record["basal_temp_delta"] = parse_float(mapped.get("basal_temp_raw", ""))

    # Scales
    record["pain_scale"] = parse_float(mapped.get("pain_scale", ""))
    record["fatigue_scale"] = parse_float(mapped.get("fatigue_scale", ""))
    record["emotional_state"] = parse_float(mapped.get("emotional_state", ""))

    # Boolean symptom flags
    record["neurological"] = parse_bool(mapped.get("neurological", ""))
    record["cognitive"] = parse_bool(mapped.get("cognitive", ""))
    record["musculature"] = parse_bool(mapped.get("musculature", ""))
    record["migraine"] = parse_bool(mapped.get("migraine", ""))
    record["pulmonary"] = parse_bool(mapped.get("pulmonary", ""))
    record["dermatological"] = parse_bool(mapped.get("dermatological", ""))
    record["mucosal"] = parse_bool(mapped.get("mucosal", ""))
    record["gastro"] = parse_bool(mapped.get("gastro", ""))

    # Flare flags
    record["strike_physical"] = parse_bool(mapped.get("strike_physical", ""))
    record["strike_environmental"] = parse_bool(mapped.get("strike_environmental", ""))
    record["flare_occurred"] = parse_bool(mapped.get("flare_occurred", ""))

    # Notes - combine original notes with preserved food/trigger fields
    original_notes = mapped.get("notes", "")
    record["notes"] = build_notes(mapped, original_notes)

    return record


# ============================================================
# Main import
# ============================================================

def run_import(csv_path: str, user_id: int = 1, dry_run: bool = False,
               preview: Optional[int] = None) -> None:
    """Run the import from CSV to database.

    Args:
        csv_path: path to the CSV file
        dry_run: if True, parse and validate but don't write to DB
        preview: if set, only process this many rows
    """
    if not os.path.exists(csv_path):
        print(f"ERROR: file not found: {csv_path}")
        sys.exit(1)

    config = load_config()
    temp_baseline = config.get("temp_baseline_f", 0.0)

    if temp_baseline == 0.0:
        print("WARNING: temp_baseline_f not set in config.json")
        print("         Basal temp delta will not be calculated correctly.")
        print("         Run setup.py to set your baseline.\n")

    print(f"sardinetracker import")
    print(f"===================")
    print(f"File:      {csv_path}")
    print(f"Baseline:  {temp_baseline}°F")
    print(f"User ID:   {user_id}")
    print(f"Dry run:   {dry_run}")
    if preview:
        print(f"Preview:   first {preview} rows only")
    print()

    if not dry_run:
        import db

    processed = 0
    imported = 0
    skipped = 0
    errors = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        # Validate that we recognize the columns
        if reader.fieldnames:
            unknown = []
            for col in reader.fieldnames:
                col_clean = col.strip()
                if col_clean not in COLUMN_MAP:
                    unknown.append(col_clean)
            if unknown:
                print(f"NOTE: {len(unknown)} unrecognized column(s) — will be ignored:")
                for u in unknown:
                    print(f"      {u}")
                print()

        for row in reader:
            processed += 1

            if preview and processed > preview:
                break

            try:
                record = process_row(row, temp_baseline)

                if record is None:
                    skipped += 1
                    continue

                if dry_run or preview:
                    print(f"  {record['date']}  "
                          f"pain={record.get('pain_scale', '—')}  "
                          f"fatigue={record.get('fatigue_scale', '—')}  "
                          f"neuro={record.get('neurological', 0)}  "
                          f"temp_delta={record.get('basal_temp_delta', '—')}")
                    imported += 1
                else:
                    db.upsert_daily_observations(user_id, record)
                    imported += 1

            except Exception as e:
                errors += 1
                print(f"  ERROR on row {processed}: {e}")
                if processed <= 3:
                    print(f"  Row data: {dict(list(row.items())[:5])}...")

    print()
    print(f"Results")
    print(f"-------")
    print(f"Rows processed:  {processed}")
    print(f"Rows imported:   {imported}")
    print(f"Rows skipped:    {skipped}  (empty rows)")
    print(f"Errors:          {errors}")

    if dry_run:
        print()
        print("Dry run complete — nothing written to database.")
        print("Remove --dry-run to perform the actual import.")
    elif not preview:
        print()
        print("Import complete.")
        print("Run the app and check the timeline to verify your data.")


# ============================================================
# Entry point
# ============================================================

def _resolve_user_id(args) -> int:
    """Resolve user_id from --user (username) or --user-id (int)."""
    if args.user:
        import db
        user = db.get_user_by_username(args.user)
        if not user:
            print(f"ERROR: no user with username '{args.user}'")
            sys.exit(1)
        return user["id"]
    return args.user_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import historical symptom tracker CSV into sardinetracker database"
    )
    parser.add_argument(
        "csv_file",
        help="Path to your exported symptom tracker CSV file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without writing to the database"
    )
    parser.add_argument(
        "--preview",
        type=int,
        metavar="N",
        help="Preview first N rows only (implies dry run display)"
    )
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument(
        "--user",
        type=str,
        help="Username to import data for"
    )
    user_group.add_argument(
        "--user-id",
        type=int,
        default=1,
        help="User ID to import data for (default: 1)"
    )

    args = parser.parse_args()
    user_id = _resolve_user_id(args)
    run_import(args.csv_file, user_id=user_id,
              dry_run=args.dry_run, preview=args.preview)