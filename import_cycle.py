#!/usr/bin/env python3
"""Import Apple Health cycle tracking data into biotracking.db.

Usage:
    python import_cycle.py [--dry-run] [--csv cycle_2024_2026.csv]
    python import_cycle.py [--dry-run] [--csv cycle_2024_2026.csv] --user-id 2

Flow value priority (when multiple rows exist for the same date):
    heavy > medium > light > spotting
"""

import argparse
import csv
import sys
from datetime import datetime

import db

FLOW_PRIORITY = {"heavy": 4, "medium": 3, "light": 2, "spotting": 1}

# Apple Health Value → biotracking period_flow
AH_FLOW_MAP = {
    "Heavy": "heavy",
    "Medium": "medium",
    "Light": "light",
    "Unspecified": "light",
}


def parse_date(start_str: str) -> str:
    """Extract YYYY-MM-DD from Apple Health datetime string."""
    # Format: '2024-03-13 12:00:00 -0500'
    return datetime.strptime(start_str.strip()[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")


def build_import_map(csv_path: str) -> dict[str, dict]:
    """Parse CSV and build {date: {period_flow, ...}} keeping highest-priority flow per day."""
    day_map: dict[str, dict] = {}

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            data_type = row.get("Data", "").strip()
            value = row.get("Value", "").strip()
            start_raw = row.get("Start", "").strip()

            if not start_raw:
                continue

            date_str = parse_date(start_raw)

            if data_type == "Menstrual Flow":
                flow = AH_FLOW_MAP.get(value)
                if not flow:
                    continue
            elif data_type == "Intermenstrual Bleeding":
                flow = "spotting"
            else:
                # Sexual Activity, Persistent Menstrual Bleeding, etc. — skip
                continue

            existing = day_map.get(date_str, {})
            existing_flow = existing.get("period_flow")

            # Keep highest-priority flow for the day
            if existing_flow is None or FLOW_PRIORITY[flow] > FLOW_PRIORITY[existing_flow]:
                existing["period_flow"] = flow

            day_map[date_str] = existing

    return day_map


def run(csv_path: str, user_id: int, dry_run: bool) -> None:
    import_map = build_import_map(csv_path)

    if not import_map:
        print("No cycle rows found in CSV.")
        return

    dates = sorted(import_map)
    print(f"Found cycle data for {len(dates)} days ({dates[0]} → {dates[-1]})")

    # Check which dates already have rows for this user
    with db.get_db() as conn:
        existing_rows = conn.execute(
            "SELECT date, period_flow FROM daily_observations WHERE user_id = ?",
            (user_id,)
        ).fetchall()
    existing_by_date = {r["date"]: r["period_flow"] for r in existing_rows}

    created = updated = skipped = 0

    for date_str, fields in sorted(import_map.items()):
        flow = fields["period_flow"]

        if date_str in existing_by_date:
            old_flow = existing_by_date[date_str]
            if old_flow == flow:
                skipped += 1
                continue
            action = f"update  {date_str}  {old_flow or '—':10s} → {flow}"
            updated += 1
        else:
            action = f"create  {date_str}  {flow}"
            created += 1

        print(f"  {'[DRY RUN] ' if dry_run else ''}{action}")

        if not dry_run:
            db.upsert_daily_observations(user_id, {"date": date_str, "period_flow": flow})

    print()
    if dry_run:
        print(f"Dry run complete. Would create {created}, update {updated}, skip {skipped} days.")
    else:
        print(f"Done. Created {created}, updated {updated}, skipped {skipped} days.")


def _resolve_user_id(args) -> int:
    """Resolve user_id from --user (username) or --user-id (int)."""
    if args.user:
        user = db.get_user_by_username(args.user)
        if not user:
            print(f"ERROR: no user with username '{args.user}'")
            sys.exit(1)
        return user["id"]
    return args.user_id


def main():
    parser = argparse.ArgumentParser(description="Import Apple Health cycle CSV into sardinetracker.")
    parser.add_argument("--csv", default="cycle_2024_2026.csv", help="Path to exported CSV file")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument("--user", type=str,
                            help="Username to import data for")
    user_group.add_argument("--user-id", type=int, default=1,
                            help="User ID to import data for (default: 1)")
    args = parser.parse_args()
    user_id = _resolve_user_id(args)

    try:
        run(args.csv, user_id, args.dry_run)
    except FileNotFoundError:
        print(f"Error: CSV file not found: {args.csv}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
