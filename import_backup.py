"""
biotracking import_backup.py
-----------------------------
Imports data from a biotracking database backup (.db file) into the
current multi-user database, scoped to a specific user.

Handles both old single-user backups (no user_id columns) and
new multi-user backups.

Usage:
    python import_backup.py biotracking_backup_20260305.db --user alaric --dry-run
    python import_backup.py biotracking_backup_20260305.db --user alaric
    python import_backup.py biotracking_backup_20260305.db --user-id 1
    python import_backup.py biotracking_backup_20260305.db --user alaric --skip-uv
"""

import argparse
import json
import os
import sqlite3
import sys

import db


# ============================================================
# Helpers
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


def _has_column(conn, table_name: str, column_name: str) -> bool:
    """Check if a table has a specific column in the backup DB."""
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the backup DB."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def _read_all(conn, table_name: str) -> list[dict]:
    """Read all rows from a table as list of dicts."""
    if not _table_exists(conn, table_name):
        return []
    rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
    cols = [desc[0] for desc in conn.execute(f"SELECT * FROM {table_name} LIMIT 0").description]
    return [{col: row[i] for i, col in enumerate(cols)} for row in rows]


# ============================================================
# Per-table import logic
# ============================================================

def import_daily_observations(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import daily_observations. Uses upsert so existing dates get merged."""
    rows = _read_all(backup_conn, "daily_observations")
    if not rows:
        return 0, 0

    # Columns to import (skip id, user_id — those are destination-specific)
    skip_cols = {"id", "user_id"}

    imported = 0
    skipped = 0

    for row in rows:
        data = {k: v for k, v in row.items() if k not in skip_cols and v is not None}
        if "date" not in data:
            skipped += 1
            continue

        if not dry_run:
            db.upsert_daily_observations(user_id, data)
        imported += 1

    return imported, skipped


def import_lab_results(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import lab_results. Skips duplicates by date+test_name+numeric_value."""
    rows = _read_all(backup_conn, "lab_results")
    if not rows:
        return 0, 0

    # Load existing labs for dedup
    existing = db.get_lab_results(user_id)
    existing_keys = {
        (r["date"], r["test_name"], r.get("numeric_value"))
        for r in existing
    }

    skip_cols = {"id", "user_id"}
    imported = 0
    skipped = 0

    for row in rows:
        key = (row.get("date"), row.get("test_name"), row.get("numeric_value"))
        if key in existing_keys:
            skipped += 1
            continue

        data = {k: v for k, v in row.items() if k not in skip_cols}
        if not data.get("date") or not data.get("test_name"):
            skipped += 1
            continue

        if not dry_run:
            try:
                db.add_lab_result(user_id, data)
            except Exception as e:
                print(f"  WARNING: lab_result {key}: {e}")
                skipped += 1
                continue
        imported += 1

    return imported, skipped


def import_ana_results(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import ana_results. Skips duplicates by date."""
    rows = _read_all(backup_conn, "ana_results")
    if not rows:
        return 0, 0

    existing = db.get_ana_results(user_id)
    existing_dates = {r["date"] for r in existing}

    imported = 0
    skipped = 0

    for row in rows:
        date_str = row.get("date")
        if not date_str or date_str in existing_dates:
            skipped += 1
            continue

        # Parse patterns — may be JSON string or plain text
        patterns_raw = row.get("patterns", "")
        if patterns_raw:
            try:
                patterns = json.loads(patterns_raw)
            except (json.JSONDecodeError, TypeError):
                patterns = [p.strip() for p in str(patterns_raw).split(",") if p.strip()]
        else:
            patterns = []

        if not dry_run:
            try:
                db.add_ana_result(
                    user_id,
                    date_str=date_str,
                    titer_integer=row.get("titer_integer"),
                    screen_result=row.get("screen_result", ""),
                    patterns=patterns,
                    provider=row.get("provider"),
                    notes=row.get("notes"),
                )
            except Exception as e:
                print(f"  WARNING: ana_result {date_str}: {e}")
                skipped += 1
                continue
        imported += 1

    return imported, skipped


def import_clinical_events(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import clinical_events. Skips duplicates by date+event_type."""
    rows = _read_all(backup_conn, "clinical_events")
    if not rows:
        return 0, 0

    existing = db.get_clinical_events(user_id)
    existing_keys = {(r["date"], r["event_type"]) for r in existing}

    skip_cols = {"id", "user_id"}
    imported = 0
    skipped = 0

    for row in rows:
        key = (row.get("date"), row.get("event_type"))
        if key[0] is None or key in existing_keys:
            skipped += 1
            continue

        data = {k: v for k, v in row.items() if k not in skip_cols}
        if not dry_run:
            try:
                db.add_clinical_event(user_id, data)
            except Exception as e:
                print(f"  WARNING: clinical_event {key}: {e}")
                skipped += 1
                continue
        imported += 1

    return imported, skipped


def import_medications(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import medications. Skips duplicates by drug_name."""
    rows = _read_all(backup_conn, "medications")
    if not rows:
        return 0, 0

    existing = db.get_all_medications(user_id)
    existing_names = {r["drug_name"] for r in existing}

    skip_cols = {"id", "user_id"}
    imported = 0
    skipped = 0

    for row in rows:
        name = row.get("drug_name")
        if not name or name in existing_names:
            skipped += 1
            continue

        data = {k: v for k, v in row.items() if k not in skip_cols}
        if not dry_run:
            try:
                db.add_medication(user_id, data)
            except Exception as e:
                print(f"  WARNING: medication {name}: {e}")
                skipped += 1
                continue
        imported += 1

    return imported, skipped


def import_clinicians(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import clinicians. Skips duplicates by name."""
    rows = _read_all(backup_conn, "clinicians")
    if not rows:
        return 0, 0

    existing = db.get_all_clinicians(user_id)
    existing_names = {r["name"] for r in existing}

    skip_cols = {"id", "user_id"}
    imported = 0
    skipped = 0

    for row in rows:
        name = row.get("name")
        if not name or name in existing_names:
            skipped += 1
            continue

        data = {k: v for k, v in row.items() if k not in skip_cols}
        if not dry_run:
            try:
                db.add_clinician(user_id, data)
            except Exception as e:
                print(f"  WARNING: clinician {name}: {e}")
                skipped += 1
                continue
        imported += 1

    return imported, skipped


def import_uv_data(backup_conn, user_id: int, dry_run: bool) -> tuple[int, int]:
    """Import uv_data. Uses the target user's location_key for storage."""
    rows = _read_all(backup_conn, "uv_data")
    if not rows:
        return 0, 0

    # Get user's location for location_key
    prefs = db.get_user_preferences(user_id)
    if prefs and prefs.get("location_lat") and prefs.get("location_lon"):
        location_key = db.make_location_key(prefs["location_lat"], prefs["location_lon"])
    else:
        location_key = "default"

    imported = 0
    skipped = 0

    for row in rows:
        date_str = row.get("date")
        if not date_str:
            skipped += 1
            continue

        # Check if we already have data for this location+date
        existing = db.get_uv_data(location_key, date_str)
        if existing and existing.get("source") == "api":
            skipped += 1
            continue

        if not dry_run:
            try:
                db.upsert_uv_data(
                    location_key=location_key,
                    date_str=date_str,
                    uv_morning=row.get("uv_morning", 0.0),
                    uv_noon=row.get("uv_noon", 0.0),
                    uv_evening=row.get("uv_evening", 0.0),
                    source=row.get("source", "backup"),
                )
            except Exception as e:
                print(f"  WARNING: uv_data {date_str}: {e}")
                skipped += 1
                continue
        imported += 1

    return imported, skipped


# ============================================================
# Main
# ============================================================

def run_import(db_path: str, user_id: int, dry_run: bool = False,
               skip_uv: bool = False) -> None:
    if not os.path.exists(db_path):
        print(f"ERROR: file not found: {db_path}")
        sys.exit(1)

    # Open backup DB read-only
    backup_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # Detect schema type
    is_multiuser = _has_column(backup_conn, "daily_observations", "user_id")

    print(f"sardinetracker backup import")
    print(f"==========================")
    print(f"Backup:   {db_path}")
    print(f"Schema:   {'multi-user' if is_multiuser else 'single-user (legacy)'}")
    print(f"User ID:  {user_id}")
    print(f"Dry run:  {dry_run}")
    print(f"Skip UV:  {skip_uv}")
    print()

    if is_multiuser:
        print("NOTE: Multi-user backup detected. All data from the backup will be")
        print("      imported into the target user's account regardless of the")
        print("      original user_id in the backup.")
        print()

    results = {}

    # Import each table
    tables = [
        ("daily_observations", import_daily_observations),
        ("lab_results",        import_lab_results),
        ("ana_results",        import_ana_results),
        ("clinical_events",    import_clinical_events),
        ("medications",        import_medications),
        ("clinicians",         import_clinicians),
    ]

    if not skip_uv:
        tables.append(("uv_data", import_uv_data))

    for table_name, import_fn in tables:
        if not _table_exists(backup_conn, table_name):
            print(f"  {table_name}: not found in backup, skipping")
            continue

        imported, skipped = import_fn(backup_conn, user_id, dry_run)
        results[table_name] = (imported, skipped)
        status = "[DRY RUN] " if dry_run else ""
        print(f"  {status}{table_name}: {imported} imported, {skipped} skipped")

    backup_conn.close()

    # Summary
    print()
    total_imported = sum(r[0] for r in results.values())
    total_skipped = sum(r[1] for r in results.values())
    print(f"Total: {total_imported} rows imported, {total_skipped} skipped")

    if dry_run:
        print()
        print("Dry run — nothing written. Remove --dry-run to import.")
    elif total_imported > 0:
        print()
        print("Import complete. Log in and verify your data.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import data from a sardinetracker database backup"
    )
    parser.add_argument("db_file", help="Path to backup .db file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to database")
    parser.add_argument("--skip-uv", action="store_true",
                        help="Skip UV data import")
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument("--user", type=str,
                            help="Username to import data for")
    user_group.add_argument("--user-id", type=int, default=1,
                            help="User ID to import data for (default: 1)")

    args = parser.parse_args()
    user_id = _resolve_user_id(args)
    run_import(args.db_file, user_id=user_id,
              dry_run=args.dry_run, skip_uv=args.skip_uv)
