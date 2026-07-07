"""
biotracking import_apple_health.py
-----------------------------------
Imports HRV, sleeping wrist temperature, and time in daylight
from a Health Export CSV file into existing daily_observations rows.

Expected columns (from Health Export app):
    Date/Time
    Apple Sleeping Wrist Temperature (degF)
    Heart Rate Variability (ms)
    Time in Daylight (min)

Wrist temperature is raw Fahrenheit - converted to delta using
temp_baseline_f from config.json.

Behavior:
    - Updates existing daily_observations rows only by default
    - Use --create-new to also insert rows for dates not yet in DB
    - Use --dry-run to preview without writing

Usage:
    python import_apple_health.py path/to/health_export.csv
    python import_apple_health.py path/to/health_export.csv --dry-run
    python import_apple_health.py path/to/health_export.csv --create-new
    python import_apple_health.py path/to/health_export.csv --user-id 2
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from typing import Optional


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        print("ERROR: config.json not found. Run setup.py first.")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


def parse_date(value: str) -> Optional[str]:
    """Parse '2025-10-10 00:00:00' to '2025-10-10'."""
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").date().isoformat()
    except ValueError:
        try:
            # Fallback: just take the date portion
            return value.strip().split(" ")[0]
        except Exception:
            return None


def parse_float(value: str) -> Optional[float]:
    if not value or not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def run_import(csv_path: str, user_id: int = 1, dry_run: bool = False,
        create_new: bool = False, overwrite: bool = False) -> None:

    if not os.path.exists(csv_path):
        print(f"ERROR: file not found: {csv_path}")
        sys.exit(1)

    config = load_config()
    temp_baseline = config.get("temp_baseline_f", 97.2)

    print(f"sardinetracker apple health import")
    print(f"================================")
    print(f"File:       {csv_path}")
    print(f"Baseline:   {temp_baseline}°F")
    print(f"User ID:    {user_id}")
    print(f"Dry run:    {dry_run}")
    print(f"Create new: {create_new}")
    print(f"Overwrite:  {overwrite}")
    print()

    if not dry_run:
        import db

    # Column name variants to handle minor differences between exports
    DATE_COLS  = ["Date/Time", "Date", "date"]
    TEMP_COLS  = ["Apple Sleeping Wrist Temperature (degF)",
                  "Sleeping Wrist Temperature (degF)",
                  "Wrist Temperature (degF)"]
    HRV_COLS   = ["Heart Rate Variability (ms)", "HRV (ms)", "HRV"]
    LIGHT_COLS = ["Time in Daylight (min)", "Daylight (min)",
                  "Sun Exposure (min)"]
    SLEEP_COLS = ["Total Sleep (hr)", "Sleep (hr)", "Hours Slept"]

    def find_col(headers: list, candidates: list) -> Optional[str]:
        for c in candidates:
            if c in headers:
                return c
        return None

    processed = 0
    updated   = 0
    created   = 0
    skipped   = 0
    errors    = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        date_col  = find_col(headers, DATE_COLS)
        temp_col  = find_col(headers, TEMP_COLS)
        hrv_col   = find_col(headers, HRV_COLS)
        light_col = find_col(headers, LIGHT_COLS)
        sleep_col = find_col(headers, SLEEP_COLS)

        if not date_col:
            print(f"ERROR: Could not find date column.")
            print(f"       Found columns: {headers}")
            sys.exit(1)

        print(f"Columns found:")
        print(f"  Date:        {date_col}")
        print(f"  Wrist temp:  {temp_col or '— not found'}")
        print(f"  HRV:         {hrv_col or '— not found'}")
        print(f"  Daylight:    {light_col or '— not found'}")
        print(f"  Sleep:       {sleep_col or '— not found'}")
        print()

        for row in reader:
            processed += 1

            date_str = parse_date(row.get(date_col, ""))
            if not date_str:
                skipped += 1
                continue

            # Parse available values
            hrv = parse_float(row.get(hrv_col, "")) if hrv_col else None
            sun_raw = parse_float(row.get(light_col, "")) if light_col else None
            sun = int(round(sun_raw * 10000)) if sun_raw is not None else None
            sleep = parse_float(row.get(sleep_col, "")) if sleep_col else None

            # Wrist temp: raw °F → delta
            temp_delta = None
            if temp_col:
                raw_temp = parse_float(row.get(temp_col, ""))
                if raw_temp is not None:
                    # Sanity check: raw temps should be in human range
                    if 95.0 <= raw_temp <= 104.0:
                        temp_delta = round(raw_temp - temp_baseline, 3)
                    else:
                        # Already a delta (some export versions)
                        temp_delta = raw_temp

            # Skip rows with no useful data
            if hrv is None and temp_delta is None and sun is None and sleep is None:
                skipped += 1
                continue

            if dry_run:
                print(f"  {date_str}  hrv={hrv}  "
                      f"temp_delta={temp_delta}  sun={sun}  sleep={sleep}")
                updated += 1
                continue

            # Check if row exists
            existing = db.get_daily_observations(user_id, date_str)

            if existing:
                # Only update fields that have new data and are currently empty
                if existing:
                    updates = {"date": date_str}
                if hrv is not None and (overwrite or not existing.get("hrv")):
                    updates["hrv"] = hrv
                if temp_delta is not None and (overwrite or not existing.get("basal_temp_delta")):
                    updates["basal_temp_delta"] = temp_delta
                if sun is not None and (overwrite or not existing.get("sun_exposure_min")):
                    updates["sun_exposure_min"] = int(sun)
                if sleep is not None and (overwrite or not existing.get("hours_slept")):
                    updates["hours_slept"] = sleep

                if len(updates) > 1:  # more than just the date key
                    try:
                        db.upsert_daily_observations(user_id, updates)
                        updated += 1
                    except Exception as e:
                        errors += 1
                        print(f"  ERROR updating {date_str}: {e}")
                else:
                    skipped += 1  # existing row already has all data

            elif create_new:
                # Create a minimal new row with just the biometric data
                new_row = {"date": date_str}
                if hrv is not None:
                    new_row["hrv"] = hrv
                if temp_delta is not None:
                    new_row["basal_temp_delta"] = temp_delta
                if sun is not None:
                    new_row["sun_exposure_min"] = int(round(sun * 10000))
                if sleep is not None:
                    new_row["hours_slept"] = sleep
                try:
                    db.upsert_daily_observations(user_id, new_row)
                    created += 1
                except Exception as e:
                    errors += 1
                    print(f"  ERROR creating {date_str}: {e}")
            else:
                skipped += 1  # no existing row, not creating new

    print()
    print(f"Results")
    print(f"-------")
    print(f"Rows processed:  {processed}")
    print(f"Rows updated:    {updated}")
    print(f"Rows created:    {created}")
    print(f"Rows skipped:    {skipped}  (no existing row, or no new data)")
    print(f"Errors:          {errors}")

    if dry_run:
        print()
        print("Dry run — nothing written. Remove --dry-run to import.")
    elif updated + created > 0:
        print()
        print("Import complete.")
        print("Reload the HRV view to see your updated data.")


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
        description="Import Apple Health biometrics from Health Export CSV"
    )
    parser.add_argument("csv_file", help="Path to Health Export CSV file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to database")
    parser.add_argument("--create-new", action="store_true",
                        help="Create new observation rows for dates not in DB")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing field values with Apple Health data")
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument("--user", type=str,
                            help="Username to import data for")
    user_group.add_argument("--user-id", type=int, default=1,
                            help="User ID to import data for (default: 1)")

    args = parser.parse_args()
    user_id = _resolve_user_id(args)
    run_import(args.csv_file, user_id=user_id, dry_run=args.dry_run,
               create_new=args.create_new, overwrite=args.overwrite)