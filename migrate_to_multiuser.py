"""
migrate_to_multiuser.py
-----------------------
One-time migration to add user_id to all data tables.

Run AFTER creating at least one user account (via create_user.py).
All existing data is assigned to user_id=1 (the owner account).

This script is idempotent — safe to run multiple times.

Usage:
    python migrate_to_multiuser.py
    python migrate_to_multiuser.py --dry-run   # show what would change without modifying
"""

import argparse
import sqlite3
import sys

DB_FILE = "biotracking.db"


def get_columns(conn, table_name: str) -> list[str]:
    """Return column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row[1] for row in rows]


def has_column(conn, table_name: str, column_name: str) -> bool:
    """Check if a table has a specific column."""
    return column_name in get_columns(conn, table_name)


def table_exists(conn, table_name: str) -> bool:
    """Check if a table exists."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def migrate_daily_observations(conn, dry_run: bool) -> bool:
    """Recreate daily_observations with user_id + composite unique key.

    Old PK: date TEXT PRIMARY KEY
    New PK: id INTEGER PRIMARY KEY AUTOINCREMENT + UNIQUE(user_id, date)
    """
    if not table_exists(conn, "daily_observations"):
        print("  daily_observations: table doesn't exist, skipping")
        return False

    # Check if already migrated (has user_id column)
    if has_column(conn, "daily_observations", "user_id"):
        print("  daily_observations: already has user_id, skipping")
        return False

    print("  daily_observations: recreating with user_id + composite key...")

    if dry_run:
        print("    [DRY RUN] would recreate table and assign all rows to user_id=1")
        return True

    # Get existing columns (minus 'date' which we handle specially)
    existing_cols = get_columns(conn, "daily_observations")

    # Build column list for the new table (preserving all existing columns)
    col_defs = ["id INTEGER PRIMARY KEY AUTOINCREMENT",
                "user_id INTEGER NOT NULL REFERENCES users(id)",
                "date TEXT NOT NULL"]
    for col in existing_cols:
        if col == "date":
            continue
        # Grab the column's type and default from pragma
        info = conn.execute("PRAGMA table_info(daily_observations)").fetchall()
        for row in info:
            if row[1] == col:
                col_type = row[2] or "TEXT"
                default = row[4]
                defn = f"{col} {col_type}"
                if default is not None:
                    defn += f" DEFAULT {default}"
                col_defs.append(defn)
                break

    col_defs.append("UNIQUE(user_id, date)")

    create_sql = f"CREATE TABLE daily_observations_new (\n    {','.join(col_defs)}\n)"
    conn.execute(create_sql)

    # Copy data, assigning user_id=1
    copy_cols = ", ".join(existing_cols)
    conn.execute(f"""
        INSERT INTO daily_observations_new (user_id, {copy_cols})
        SELECT 1, {copy_cols} FROM daily_observations
    """)

    row_count = conn.execute("SELECT COUNT(*) FROM daily_observations").fetchone()[0]
    conn.execute("DROP TABLE daily_observations")
    conn.execute("ALTER TABLE daily_observations_new RENAME TO daily_observations")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_obs_user ON daily_observations(user_id)")

    print(f"    migrated {row_count} rows, all assigned to user_id=1")
    return True


def migrate_uv_data(conn, dry_run: bool) -> bool:
    """Recreate uv_data with location_key for multi-location support.

    Old PK: date TEXT PRIMARY KEY
    New:    id PK + UNIQUE(location_key, date)
    """
    if not table_exists(conn, "uv_data"):
        print("  uv_data: table doesn't exist, skipping")
        return False

    if has_column(conn, "uv_data", "location_key"):
        print("  uv_data: already has location_key, skipping")
        return False

    print("  uv_data: recreating with location_key...")

    if dry_run:
        print("    [DRY RUN] would recreate table with location_key column")
        return True

    conn.execute("""
        CREATE TABLE uv_data_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            location_key TEXT NOT NULL DEFAULT 'default',
            date         TEXT NOT NULL,
            uv_morning   REAL,
            uv_noon      REAL,
            uv_evening   REAL,
            source       TEXT DEFAULT 'api',
            UNIQUE(location_key, date)
        )
    """)

    # Copy existing data with a default location_key
    row_count = conn.execute("SELECT COUNT(*) FROM uv_data").fetchone()[0]
    conn.execute("""
        INSERT INTO uv_data_new (location_key, date, uv_morning, uv_noon, uv_evening, source)
        SELECT 'default', date, uv_morning, uv_noon, uv_evening, source
        FROM uv_data
    """)

    conn.execute("DROP TABLE uv_data")
    conn.execute("ALTER TABLE uv_data_new RENAME TO uv_data")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uv_location ON uv_data(location_key)")

    print(f"    migrated {row_count} rows with location_key='default'")
    return True


def add_user_id_column(conn, table_name: str, dry_run: bool) -> bool:
    """Add user_id column to a table and set all existing rows to user_id=1."""
    if not table_exists(conn, table_name):
        print(f"  {table_name}: table doesn't exist, skipping")
        return False

    if has_column(conn, table_name, "user_id"):
        print(f"  {table_name}: already has user_id, skipping")
        return False

    print(f"  {table_name}: adding user_id column...")

    if dry_run:
        print(f"    [DRY RUN] would add user_id and set all rows to 1")
        return True

    # Add column with default 1 first (so existing rows get the value)
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER DEFAULT 1 REFERENCES users(id)")
    row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_user ON {table_name}(user_id)")

    print(f"    added user_id to {row_count} rows (all set to 1)")
    return True


def verify_migration(conn):
    """Check that all tables have the expected multi-user columns."""
    print("\n--- Verification ---")
    issues = []

    # daily_observations should have user_id
    if not has_column(conn, "daily_observations", "user_id"):
        issues.append("daily_observations missing user_id")

    # uv_data should have location_key
    if not has_column(conn, "uv_data", "location_key"):
        issues.append("uv_data missing location_key")

    # These tables should have user_id
    tables_needing_user_id = [
        "lab_results", "ana_results", "clinical_events",
        "medications", "clinicians", "bc_history",
        "taper_schedules", "scheduled_doses"
    ]
    for table in tables_needing_user_id:
        if table_exists(conn, table) and not has_column(conn, table, "user_id"):
            issues.append(f"{table} missing user_id")

    if issues:
        print("ISSUES FOUND:")
        for issue in issues:
            print(f"  - {issue}")
        return False
    else:
        print("All tables have multi-user columns. Migration complete.")
        return True


def main():
    parser = argparse.ArgumentParser(description="Migrate sardinetracker DB to multi-user")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying the database")
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN — no changes will be made ===\n")

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = OFF")  # Off during migration for table recreation

    # Verify at least one user exists
    try:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except sqlite3.OperationalError:
        print("ERROR: 'users' table doesn't exist. Run setup.py first, then create_user.py.")
        sys.exit(1)

    if user_count == 0:
        print("ERROR: No users found. Create at least one user with create_user.py first.")
        print("All existing data will be assigned to user_id=1 (the first user created).")
        sys.exit(1)

    print(f"Found {user_count} user(s). Existing data will be assigned to user_id=1.\n")

    changed = False
    print("--- Table migrations ---")

    # 1. daily_observations — needs full table recreation
    changed |= migrate_daily_observations(conn, args.dry_run)

    # 2. uv_data — needs full table recreation for location_key
    changed |= migrate_uv_data(conn, args.dry_run)

    # 3. Simple ALTER TABLE additions for remaining tables
    simple_tables = [
        "lab_results",
        "ana_results",
        "clinical_events",
        "medications",
        "clinicians",
        "bc_history",
        "taper_schedules",
        "scheduled_doses",
    ]
    for table in simple_tables:
        changed |= add_user_id_column(conn, table, args.dry_run)

    if not args.dry_run:
        conn.commit()

    if not changed:
        print("\nNo changes needed — migration already applied.")
    else:
        if args.dry_run:
            print("\n[DRY RUN] Changes above would be applied. Run without --dry-run to execute.")
        else:
            print("\nMigration applied successfully.")

    verify_migration(conn)
    conn.close()


if __name__ == "__main__":
    main()
