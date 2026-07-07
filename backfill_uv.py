"""
biotracking backfill_uv.py
--------------------------
Fetches historical UV index data from Visual Crossing for all dates
in daily_observations that don't yet have UV data stored.

Visual Crossing is used for historical backfill only (>16 days ago).
Current and recent dates use Open-Meteo via uv_fetcher.py.

Requires: "visual_crossing_key" in config.json

Usage:
    python backfill_uv.py --user alaric
    python backfill_uv.py --user alaric --force
    python backfill_uv.py --user-id 1 --dry-run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

import db


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
# Visual Crossing API
# ============================================================

VC_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"

# Hours to sample - same as Open-Meteo for consistency
HOUR_MORNING = 9
HOUR_NOON    = 12
HOUR_EVENING = 17


def fetch_uv_range_visual_crossing(
    start_date: str,
    end_date: str,
    lat: float,
    lon: float,
    api_key: str,
    timezone: str = "America/Chicago"
) -> list[dict]:
    """Fetch hourly UV index from Visual Crossing for a date range.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
        lat:        latitude
        lon:        longitude
        api_key:    Visual Crossing API key from config.json
        timezone:   timezone string

    Returns:
        List of dicts: {date, uv_morning, uv_noon, uv_evening, source}
    """
    location = f"{lat},{lon}"
    url = f"{VC_BASE}/{location}/{start_date}/{end_date}"

    params = {
        "unitGroup":    "us",
        "elements":     "datetime,uvindex",
        "include":      "hours",
        "key":          api_key,
        "contentType":  "json",
        "timezone":     timezone,
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        if response.status_code == 401:
            print("ERROR: Visual Crossing API key invalid or not in config.json.")
            print("       Check your key at visualcrossing.com/account")
        elif response.status_code == 429:
            print("ERROR: Visual Crossing rate limit hit.")
            print("       Free tier allows 1000 records/day. Try again tomorrow.")
        else:
            print(f"ERROR: HTTP {response.status_code} from Visual Crossing: {e}")
        return []
    except requests.exceptions.ConnectionError:
        print("ERROR: No internet connection.")
        return []
    except Exception as e:
        print(f"ERROR: Unexpected error fetching from Visual Crossing: {e}")
        return []

    results = []
    days = data.get("days", [])

    for day in days:
        date_str = day.get("datetime")
        if not date_str:
            continue

        hours = day.get("hours", [])

        # Build hour -> uvindex lookup
        uv_by_hour = {}
        for h in hours:
            dt = h.get("datetime", "")  # "09:00:00"
            try:
                hour_int = int(dt.split(":")[0])
                uv_by_hour[hour_int] = h.get("uvindex", 0.0) or 0.0
            except (ValueError, IndexError):
                continue

        def get_hour(h: int) -> float:
            return round(float(uv_by_hour.get(h, 0.0)), 2)

        results.append({
            "date":       date_str,
            "uv_morning": get_hour(HOUR_MORNING),
            "uv_noon":    get_hour(HOUR_NOON),
            "uv_evening": get_hour(HOUR_EVENING),
            "source":     "visual_crossing",
        })

    return results


# ============================================================
# Main backfill
# ============================================================

def run_backfill(user_id: int, dry_run: bool = False, force: bool = False) -> None:
    """Fetch and store UV data for all observation dates missing it."""

    config = load_config()

    api_key = config.get("visual_crossing_key", "").strip()
    if not api_key:
        print("ERROR: 'visual_crossing_key' not found in config.json")
        print("       Sign up free at visualcrossing.com and add your key.")
        sys.exit(1)

    # Use user preferences for location, fall back to config
    prefs = db.get_user_preferences(user_id)
    lat = (prefs or {}).get("location_lat") or config.get("location_lat")
    lon = (prefs or {}).get("location_lon") or config.get("location_lon")
    timezone = config.get("timezone", "America/Chicago")

    if not lat or not lon:
        print("ERROR: location_lat/location_lon not set in config.json or user preferences")
        sys.exit(1)

    location_key = db.make_location_key(float(lat), float(lon))

    # Get all observation dates for this user
    observations = db.get_all_daily_observations(user_id)
    if not observations:
        print("No daily observations found. Import your tracker data first.")
        return

    all_dates = [obs["date"] for obs in observations]

    # Determine which dates need UV data
    if force:
        dates_needed = all_dates
    else:
        dates_needed = []
        for d in all_dates:
            existing = db.get_uv_data(location_key, d)
            if not existing:
                dates_needed.append(d)

    if not dates_needed:
        print("UV data already present for all observation dates.")
        print("Use --force to re-fetch everything.")
        return

    start_date = min(dates_needed)
    end_date   = max(dates_needed)

    # Estimate API cost
    # Visual Crossing counts each day of hourly data as 24 records
    num_days = (
        datetime.strptime(end_date, "%Y-%m-%d") -
        datetime.strptime(start_date, "%Y-%m-%d")
    ).days + 1
    estimated_records = num_days * 24

    print(f"sardinetracker UV backfill")
    print(f"========================")
    print(f"Dates needing UV:  {len(dates_needed)}")
    print(f"Date range:        {start_date} to {end_date}")
    print(f"Est. API records:  ~{estimated_records} (free tier: 1000/day)")
    print(f"Dry run:           {dry_run}")
    print(f"Force re-fetch:    {force}")
    print()

    if estimated_records > 900:
        print(f"WARNING: This request may use most or all of your daily free tier.")
        confirm = input("Continue? (y/n): ").strip().lower()
        if confirm != "y":
            print("Backfill cancelled.")
            return
        print()

    if dry_run:
        print(f"Dry run — would fetch UV for {len(dates_needed)} dates")
        print(f"from Visual Crossing ({start_date} to {end_date})")
        print(f"No data written.")
        return

    print(f"Fetching from Visual Crossing...")

    results = fetch_uv_range_visual_crossing(
        start_date=start_date,
        end_date=end_date,
        lat=lat,
        lon=lon,
        api_key=api_key,
        timezone=timezone,
    )

    if not results:
        print("No data returned. Check errors above.")
        return

    # Build lookup of fetched results
    fetched_by_date = {r["date"]: r for r in results}

    stored = 0
    missing = 0

    for date_str in dates_needed:
        uv = fetched_by_date.get(date_str)
        if not uv:
            missing += 1
            continue

        db.upsert_uv_data(
            location_key=location_key,
            date_str=uv["date"],
            uv_morning=uv["uv_morning"],
            uv_noon=uv["uv_noon"],
            uv_evening=uv["uv_evening"],
            source="visual_crossing",
        )
        stored += 1

    print()
    print(f"Results")
    print(f"-------")
    print(f"Dates fetched:   {len(results)}")
    print(f"Dates stored:    {stored}")
    print(f"Dates missing:   {missing}  (not in API response)")
    print()

    if stored > 0:
        print("Backfill complete.")
        print("Reload the timeline view to see UV data for your full history.")
    else:
        print("Nothing stored. Check errors above.")


# ============================================================
# Entry point
# ============================================================

def _resolve_user_id(args) -> int:
    """Resolve user_id from --user (username) or --user-id (int)."""
    if args.user:
        user = db.get_user_by_username(args.user)
        if not user:
            print(f"ERROR: no user with username '{args.user}'")
            sys.exit(1)
        return user["id"]
    return args.user_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill historical UV data from Visual Crossing"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched without writing to database"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch UV even for dates that already have data stored"
    )
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument("--user", type=str,
                            help="Username to backfill UV data for")
    user_group.add_argument("--user-id", type=int, default=1,
                            help="User ID to backfill UV data for (default: 1)")

    args = parser.parse_args()
    user_id = _resolve_user_id(args)
    run_backfill(user_id=user_id, dry_run=args.dry_run, force=args.force)