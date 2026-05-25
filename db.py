"""
biotracking db.py
-----------------
All database logic lives here. Nothing else in the application
touches SQLite directly. Flask routes call these functions only.

All dates are stored and accepted as strings in YYYY-MM-DD format.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Optional


DB_FILE = "biotracking.db"


# ============================================================
# Connection management
# ============================================================

@contextmanager
def get_db():
    """Context manager for database connections.
    Automatically commits on success and rolls back on error.
    
    Usage:
        with get_db() as conn:
            conn.execute(...)
    """
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def today():
    """Return today's date as YYYY-MM-DD string."""
    return date.today().isoformat()


# ============================================================
# Idempotent schema migrations
# ============================================================
# Called once at app startup to bring an existing DB up to the schema the
# running code expects. Safe to call repeatedly. Prevents the "no such table"
# / "no such column" class of errors when the app adds new features that
# require schema changes but the DB was created before that feature landed.
#
# Adding a new migration: append a block inside run_migrations() that uses
# _table_missing() or _column_missing() as the gate. Each applied migration
# increments the returned count so callers can log what changed.

def _table_missing(cursor, table: str) -> bool:
    row = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return row is None


def _column_missing(cursor, table: str, column: str) -> bool:
    rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
    return all(r["name"] != column for r in rows)


def run_migrations() -> int:
    """Apply idempotent schema migrations. Returns the number of migrations
    that actually performed a change (0 if everything was already up to date).

    Call once at app startup.
    """
    applied = 0
    with get_db() as conn:
        c = conn.cursor()

        # ---- medication_events (added 2026-04-19 for /interventions view) ----
        if _table_missing(c, "medication_events"):
            c.execute("""
                CREATE TABLE medication_events (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id        INTEGER NOT NULL REFERENCES users(id),
                    medication_id  INTEGER NOT NULL,
                    event_date     TEXT NOT NULL,
                    event_type     TEXT NOT NULL,
                    severity       INTEGER,
                    note           TEXT,
                    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (medication_id) REFERENCES medications(id) ON DELETE CASCADE
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_med_events_med "
                "ON medication_events(medication_id, event_date)"
            )
            applied += 1

        # ---- uv_sensor_readings (added 2026-04-24 for uv-wearable ingest) ----
        # Per-sample UV readings from the wearable. Different grain than uv_data
        # (which is daily morning/noon/evening summaries from a forecast API).
        # ts is NULL when we can't anchor reliably (rows from a previous boot
        # that never synced before the device rebooted).
        if _table_missing(c, "uv_sensor_readings"):
            c.execute("""
                CREATE TABLE uv_sensor_readings (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       INTEGER NOT NULL REFERENCES users(id),
                    boot_id       INTEGER NOT NULL,
                    ms_since_boot INTEGER NOT NULL,
                    ts            TEXT,
                    ts_confidence TEXT,
                    uva           INTEGER,
                    uvb           INTEGER,
                    comp1         INTEGER,
                    comp2         INTEGER,
                    uv_index      REAL,
                    batt_mv       INTEGER,
                    event_label   TEXT,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(user_id, boot_id, ms_since_boot)
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_uv_sensor_user_ts "
                "ON uv_sensor_readings(user_id, ts)"
            )
            applied += 1

    return applied


# ============================================================
# Users
# ============================================================

def get_user_by_id(user_id: int) -> Optional[dict]:
    """Fetch a user by primary key. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    """Fetch a user by username. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def create_user(username: str, display_name: str, password_hash: str,
                is_admin: bool = False) -> int:
    """Create a new user. Returns the new user's id."""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO users (username, display_name, password_hash, is_admin)
               VALUES (?, ?, ?, ?)""",
            (username, display_name, password_hash, int(is_admin))
        )
        return cursor.lastrowid


def get_all_users() -> list:
    """Return all users as a list of dicts."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users"
        ).fetchall()
        return [dict(r) for r in rows]


def update_user_password(user_id: int, password_hash: str) -> None:
    """Update a user's password hash."""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id)
        )


def delete_user(user_id: int) -> None:
    """Delete a user and all their data across all tables."""
    with get_db() as conn:
        # Delete from all user-scoped tables
        for table in [
            "daily_observations", "lab_results", "ana_results",
            "clinical_events", "medications", "clinicians",
            "bc_history", "taper_schedules", "scheduled_doses",
            "user_preferences",
        ]:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ============================================================
# User preferences
# ============================================================

def get_user_preferences(user_id: int) -> Optional[dict]:
    """Fetch all preferences for a user. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def upsert_user_preferences(user_id: int, prefs: dict) -> None:
    """Insert or update preferences for a user.
    Only updates keys present in prefs dict; leaves others unchanged.
    """
    with get_db() as conn:
        existing = conn.execute(
            "SELECT user_id FROM user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()

        if existing:
            # Update only the provided keys
            sets = []
            vals = []
            for key, val in prefs.items():
                if key == 'user_id':
                    continue
                sets.append(f"{key} = ?")
                vals.append(val)
            if sets:
                vals.append(user_id)
                conn.execute(
                    f"UPDATE user_preferences SET {', '.join(sets)} WHERE user_id = ?",
                    vals
                )
        else:
            # Insert new row
            prefs['user_id'] = user_id
            cols = ', '.join(prefs.keys())
            placeholders = ', '.join('?' for _ in prefs)
            conn.execute(
                f"INSERT INTO user_preferences ({cols}) VALUES ({placeholders})",
                list(prefs.values())
            )


def get_user_preference(user_id: int, key: str, default=None):
    """Get a single preference value for a user."""
    prefs = get_user_preferences(user_id)
    if prefs is None:
        return default
    return prefs.get(key, default)


def get_users_with_ntfy() -> list[dict]:
    """Return all users who have ntfy_topic configured in their preferences.
    Includes user_id, ntfy_topic, ntfy_server, and location fields.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.id AS user_id, u.username, u.display_name,
                   p.ntfy_topic, p.ntfy_server, p.location_lat, p.location_lon,
                   p.timezone, p.last_flare_alert_date, p.last_uv_alert_date,
                   p.reminder_hours, p.last_logged_at, p.last_reminder_date,
                   p.last_period_nudge_date, p.track_cycle
            FROM users u
            JOIN user_preferences p ON u.id = p.user_id
            WHERE p.ntfy_topic IS NOT NULL AND p.ntfy_topic != ''
        """).fetchall()
        return [dict(r) for r in rows]


def get_distinct_user_locations() -> list[dict]:
    """Return distinct (location_lat, location_lon, timezone) from user_preferences.
    Each entry includes the list of user_ids at that location.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT location_lat, location_lon, timezone, GROUP_CONCAT(user_id) AS user_ids
            FROM user_preferences
            WHERE location_lat IS NOT NULL AND location_lon IS NOT NULL
            GROUP BY ROUND(location_lat, 2), ROUND(location_lon, 2)
        """).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["user_ids"] = [int(uid) for uid in d["user_ids"].split(",")]
            results.append(d)
        return results


# ============================================================
# daily_observations
# ============================================================

def upsert_daily_observations(user_id: int, data: dict) -> bool:
    """Insert or update a daily observation row.

    Args:
        user_id: the owning user's id.
        data: dict with keys matching daily_observations columns.
              'date' is required. All other fields are optional.

    Returns:
        True on success.
    """
    if "date" not in data:
        raise ValueError("daily observation requires a 'date' field")

    fields = [
        "date", "steps", "hours_slept", "hrv", "hrv_rmssd", "resting_heart_rate",
        "spo2", "respiratory_rate", "basal_temp_delta",
        "sun_exposure_min", "pain_scale", "fatigue_scale",
        "emotional_state", "emotional_notes",
        "neurological", "neuro_notes",
        "cognitive", "cognitive_notes",
        "musculature", "musculature_notes",
        "migraine", "migraine_notes",
        "pulmonary", "pulmonary_notes",
        "rheumatic", "rheumatic_notes",
        "dermatological", "derm_notes",
        "mucosal", "mucosal_notes",
        "gastro", "gastro_notes",
        "strike_physical", "strike_environmental", "flare_occurred",
        "notes",
        "period_flow", "cramping", "cycle_notes",
        "stayed_indoors", "uv_protection_level",
        "flare_severity",
    ]

    # Only include fields present in data, plus user_id
    present = {"user_id": user_id}
    present.update({k: data[k] for k in fields if k in data})
    columns = ", ".join(present.keys())
    placeholders = ", ".join(["?" for _ in present])
    updates = ", ".join([f"{k}=excluded.{k}" for k in present if k not in ("date", "user_id")])

    sql = f"""
        INSERT INTO daily_observations ({columns})
        VALUES ({placeholders})
        ON CONFLICT(user_id, date) DO UPDATE SET {updates}
    """

    with get_db() as conn:
        conn.execute(sql, list(present.values()))

    return True


def record_health_sync_event(user_id: int, posted_at: str, metric_date: str,
                              fields_updated: list, payload: dict) -> None:
    """Append a row to health_sync_events for the /api/health-sync POST audit log."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO health_sync_events
               (user_id, posted_at, metric_date, fields_updated, payload_json)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, posted_at, metric_date,
             json.dumps(fields_updated), json.dumps(payload))
        )


def get_recent_health_sync_events(user_id: int, limit: int = 10) -> list[dict]:
    """Fetch the N most recent health sync events for a user, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, posted_at, metric_date, fields_updated, payload_json
               FROM health_sync_events
               WHERE user_id = ?
               ORDER BY posted_at DESC
               LIMIT ?""",
            (user_id, limit)
        ).fetchall()
    events = []
    for row in rows:
        d = dict(row)
        try:
            d["fields_updated"] = json.loads(d["fields_updated"])
        except (TypeError, ValueError):
            d["fields_updated"] = []
        try:
            d["payload"] = json.loads(d["payload_json"])
        except (TypeError, ValueError):
            d["payload"] = {}
        del d["payload_json"]
        events.append(d)
    return events


def get_daily_observations(user_id: int, date_str: str) -> Optional[dict]:
    """Fetch a single daily observation by user and date."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_observations WHERE user_id = ? AND date = ?",
            (user_id, date_str)
        ).fetchone()
    return dict(row) if row else None


def get_daily_observations_range(user_id: int, start_date: str, end_date: str) -> list[dict]:
    """Fetch all daily observations between two dates inclusive for a user."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM daily_observations
               WHERE user_id = ? AND date BETWEEN ? AND ?
               ORDER BY date ASC""",
            (user_id, start_date, end_date)
        ).fetchall()
    return [dict(row) for row in rows]


def get_cycle_data(user_id: int, start_date: str, end_date: str) -> list[dict]:
    """Fetch observations for the cycle calendar (period, BBT, flare, cramping)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT date, period_flow, cramping, cycle_notes,
                      basal_temp_delta, flare_occurred
               FROM daily_observations
               WHERE user_id = ? AND date BETWEEN ? AND ?
               ORDER BY date""",
            (user_id, start_date, end_date)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_daily_observations(user_id: int) -> list[dict]:
    """Fetch all daily observations ordered by date for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_observations WHERE user_id = ? ORDER BY date ASC",
            (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]

def get_all_observations(user_id: int):
    """Get all daily observations for a user."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT * FROM daily_observations
            WHERE user_id = ?
            ORDER BY date DESC
        """, (user_id,))
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


# ============================================================
# uv_data
# ============================================================

def make_location_key(lat: float, lon: float) -> str:
    """Create a location key from lat/lon, rounded to 2 decimal places (~1km)."""
    return f"{lat:.2f},{lon:.2f}"


def upsert_uv_data(location_key: str, date_str: str, uv_morning: float,
                   uv_noon: float, uv_evening: float,
                   source: str = "api",
                   cloud_cover_pct: float = None,
                   temperature_high: float = None,
                   weather_summary: str = None) -> bool:
    """Insert or update UV data for a given location + date."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO uv_data (location_key, date, uv_morning, uv_noon, uv_evening,
                                 source, cloud_cover_pct, temperature_high, weather_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(location_key, date) DO UPDATE SET
                uv_morning=excluded.uv_morning,
                uv_noon=excluded.uv_noon,
                uv_evening=excluded.uv_evening,
                source=excluded.source,
                cloud_cover_pct=excluded.cloud_cover_pct,
                temperature_high=excluded.temperature_high,
                weather_summary=excluded.weather_summary
        """, (location_key, date_str, uv_morning, uv_noon, uv_evening, source,
              cloud_cover_pct, temperature_high, weather_summary))
    return True


def get_uv_data(location_key: str, date_str: str) -> Optional[dict]:
    """Fetch UV data for a specific location + date."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM uv_data WHERE location_key = ? AND date = ?",
            (location_key, date_str)
        ).fetchone()
    return dict(row) if row else None


def get_uv_data_range(location_key: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch UV data for a location over a date range."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM uv_data
               WHERE location_key = ? AND date BETWEEN ? AND ?
               ORDER BY date ASC""",
            (location_key, start_date, end_date)
        ).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# lab_results
# ============================================================

def add_lab_result(user_id: int, data: dict) -> int:
    """Insert a lab result. Returns the new row id."""
    required = {"date", "test_name"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"lab_result missing required fields: {missing}")

    if not data.get("numeric_value") and not data.get("qualitative_result"):
        raise ValueError("lab_result requires numeric_value or qualitative_result")

    fields = [
        "date", "test_name", "numeric_value", "unit",
        "qualitative_result", "reference_range", "flag",
        "provider", "lab_facility", "notes"
    ]
    present = {"user_id": user_id}
    present.update({k: data[k] for k in fields if k in data})
    columns = ", ".join(present.keys())
    placeholders = ", ".join(["?" for _ in present])

    with get_db() as conn:
        cursor = conn.execute(
            f"INSERT INTO lab_results ({columns}) VALUES ({placeholders})",
            list(present.values())
        )
        return cursor.lastrowid


def get_lab_results(user_id: int, test_name: Optional[str] = None,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> list[dict]:
    """Fetch lab results for a user, optionally filtered by test name and date range."""
    conditions = ["user_id = ?"]
    params = [user_id]

    if test_name:
        conditions.append("test_name = ?")
        params.append(test_name)
    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)

    where = f"WHERE {' AND '.join(conditions)}"

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM lab_results {where} ORDER BY date ASC, test_name ASC",
            params
        ).fetchall()
    return [dict(row) for row in rows]


def get_lab_test_names(user_id: int) -> list[str]:
    """Return a sorted list of all distinct test names for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT test_name FROM lab_results WHERE user_id = ? ORDER BY test_name ASC",
            (user_id,)
        ).fetchall()
    return [row[0] for row in rows]


def delete_lab_result(user_id: int, result_id: int) -> bool:
    """Delete a lab result by id, scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM lab_results WHERE id = ? AND user_id = ?",
                     (result_id, user_id))
    return True


# ============================================================
# ana_results
# ============================================================

def add_ana_result(user_id: int, date_str: str, titer_integer: Optional[int],
                   screen_result: str, patterns: list[str],
                   provider: Optional[str] = None,
                   notes: Optional[str] = None) -> int:
    """Insert an ANA result. Patterns stored as JSON array.

    Args:
        user_id: owning user's id.
        date_str: YYYY-MM-DD
        titer_integer: titer as integer (40, 80, 160, etc.)
        screen_result: 'positive' or 'negative'
        patterns: list of AC codes e.g. ['AC-4', 'AC-29']
        provider: ordering provider name
        notes: free text

    Returns:
        New row id.
    """
    patterns_json = json.dumps(patterns)
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO ana_results
                (user_id, date, titer_integer, screen_result, patterns, provider, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, date_str, titer_integer, screen_result, patterns_json, provider, notes))
        return cursor.lastrowid


def get_ana_results(user_id: int, start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> list[dict]:
    """Fetch ANA results for a user, with patterns deserialized to lists."""
    conditions = ["user_id = ?"]
    params = [user_id]
    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)

    where = f"WHERE {' AND '.join(conditions)}"

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM ana_results {where} ORDER BY date ASC",
            params
        ).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["patterns"] = json.loads(d["patterns"]) if d["patterns"] else []
        results.append(d)
    return results


# ============================================================
# clinical_events
# ============================================================

def add_clinical_event(user_id: int, data: dict) -> int:
    """Insert a clinical event. Returns new row id."""
    required = {"date", "event_type"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"clinical_event missing required fields: {missing}")

    fields = ["date", "event_type", "provider", "facility", "notes", "follow_up_date"]
    present = {"user_id": user_id}
    present.update({k: data[k] for k in fields if k in data})
    columns = ", ".join(present.keys())
    placeholders = ", ".join(["?" for _ in present])

    with get_db() as conn:
        cursor = conn.execute(
            f"INSERT INTO clinical_events ({columns}) VALUES ({placeholders})",
            list(present.values())
        )
        return cursor.lastrowid


def get_clinical_events(user_id: int, event_type: Optional[str] = None,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None) -> list[dict]:
    """Fetch clinical events for a user, optionally filtered."""
    conditions = ["user_id = ?"]
    params: list = [user_id]
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)

    where = f"WHERE {' AND '.join(conditions)}"

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM clinical_events {where} ORDER BY date ASC",
            params
        ).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# medications
# ============================================================

def add_medication(user_id: int, data: dict) -> int:
    """Add a new medication for a user."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO medications (
                user_id, drug_name, dose, unit, frequency, route, category,
                indication, start_date, end_date, notes,
                is_primary_intervention, is_secondary_intervention
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            data.get("drug_name"),
            data.get("dose"),
            data.get("unit"),
            data.get("frequency"),
            data.get("route"),
            data.get("category"),
            data.get("indication"),
            data.get("start_date"),
            data.get("end_date"),
            data.get("notes"),
            1 if data.get("is_primary_intervention") else 0,
            1 if data.get("is_secondary_intervention") else 0,
        ))
        return c.lastrowid


def end_medication(user_id: int, med_id: int, end_date: str) -> bool:
    """Mark a medication course as ended, scoped to user."""
    with get_db() as conn:
        conn.execute(
            "UPDATE medications SET end_date = ? WHERE id = ? AND user_id = ?",
            (end_date, med_id, user_id)
        )
    return True


def get_active_medications(user_id: int, as_of_date: Optional[str] = None) -> list[dict]:
    """Return medications active on a given date for a user (default: today)."""
    as_of = as_of_date or today()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM medications
            WHERE user_id = ?
              AND start_date <= ?
              AND (end_date IS NULL OR end_date >= ?)
            ORDER BY drug_name ASC
        """, (user_id, as_of, as_of)).fetchall()
    return [dict(row) for row in rows]


def get_all_medications(user_id: int) -> list[dict]:
    """Return full medication history for a user ordered by start date."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM medications WHERE user_id = ? ORDER BY start_date ASC",
            (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_medication(user_id: int, med_id: int) -> Optional[dict]:
    """Return a single medication by id, scoped to user."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM medications WHERE id = ? AND user_id = ?",
            (med_id, user_id)
        ).fetchone()
    return dict(row) if row else None


# ============================================================
# medication_events
# Dated observations about a medication (side effects, rebound, etc.)
# ============================================================

def add_medication_event(user_id: int, medication_id: int, event_date: str,
                         event_type: str, severity: Optional[int] = None,
                         note: Optional[str] = None) -> int:
    """Add a medication event (side effect, rebound, dose change, note)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO medication_events
                (user_id, medication_id, event_date, event_type, severity, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, medication_id, event_date, event_type, severity, note))
        return c.lastrowid


def get_medication_events(user_id: int, medication_id: int) -> list[dict]:
    """Return events for one medication, newest first, scoped to user."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM medication_events
            WHERE medication_id = ? AND user_id = ?
            ORDER BY event_date DESC, id DESC
        """, (medication_id, user_id)).fetchall()
    return [dict(row) for row in rows]


def get_medication_event(user_id: int, event_id: int) -> Optional[dict]:
    """Return a single event by id, scoped to user."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM medication_events WHERE id = ? AND user_id = ?",
            (event_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def update_medication_event(user_id: int, event_id: int, event_date: str,
                            event_type: str, severity: Optional[int],
                            note: Optional[str]) -> bool:
    """Update an existing medication event, scoped to user."""
    with get_db() as conn:
        conn.execute("""
            UPDATE medication_events
            SET event_date = ?, event_type = ?, severity = ?, note = ?
            WHERE id = ? AND user_id = ?
        """, (event_date, event_type, severity, note, event_id, user_id))
    return True


def delete_medication_event(user_id: int, event_id: int) -> bool:
    """Delete a medication event, scoped to user."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM medication_events WHERE id = ? AND user_id = ?",
            (event_id, user_id)
        )
    return True


# ============================================================
# Full text search across note fields
# ============================================================

def search_notes(user_id: int, query: str) -> list[dict]:
    """Search across all note fields in daily_observations for a user.

    Returns matching rows with date, source field, and matching text.
    Uses SQLite LIKE for broad compatibility.
    """
    patterns = f"%{query}%"
    results = []

    note_fields = [
        ("neuro_notes", "neurological"),
        ("cognitive_notes", "cognitive"),
        ("musculature_notes", "musculature"),
        ("migraine_notes", "migraine"),
        ("air_hunger_notes", "air hunger"),
        ("rheumatic_notes", "rheumatic"),
        ("word_loss_notes", "word loss"),
        ("derm_notes", "dermatological"),
        ("notes", "general"),
    ]

    with get_db() as conn:
        for field, label in note_fields:
            rows = conn.execute(
                f"""SELECT date, '{label}' as category, {field} as matched_text
                    FROM daily_observations
                    WHERE user_id = ? AND {field} LIKE ?
                    ORDER BY date ASC""",
                (user_id, patterns)
            ).fetchall()
            results.extend([dict(row) for row in rows])

        # Also search clinical event notes
        rows = conn.execute(
            """SELECT date, event_type as category, notes as matched_text
               FROM clinical_events
               WHERE user_id = ? AND notes LIKE ?
               ORDER BY date ASC""",
            (user_id, patterns)
        ).fetchall()
        results.extend([dict(row) for row in rows])

    # Sort all results by date
    results.sort(key=lambda x: x["date"])
    return results


# ============================================================
# Timeline query - joins everything for the timeline view
# ============================================================

def get_timeline_data(user_id: int, location_key: str,
                      start_date: str, end_date: str) -> dict:
    """Fetch all data needed for the timeline view in one call.

    Returns a dict with keys:
        - daily: list of daily_observations
        - uv: list of uv_data
        - labs: list of lab_results
        - ana: list of ana_results
        - events: list of clinical_events
        - medications: list of medications active during this period
    """
    return {
        "daily": get_daily_observations_range(user_id, start_date, end_date),
        "uv": get_uv_data_range(location_key, start_date, end_date),
        "labs": get_lab_results(user_id, start_date=start_date, end_date=end_date),
        "ana": get_ana_results(user_id, start_date=start_date, end_date=end_date),
        "events": get_clinical_events(user_id, start_date=start_date, end_date=end_date),
        "medications": get_all_medications(user_id),  # filtered in frontend by date
    }

def update_lab_result(user_id: int, lab_id: int, date: str, test_name: str,
                      numeric_value: Optional[float] = None,
                      unit: Optional[str] = None,
                      qualitative_result: Optional[str] = None,
                      reference_range: Optional[str] = None,
                      flag: Optional[str] = None,
                      provider: Optional[str] = None,
                      lab_facility: Optional[str] = None,
                      notes: Optional[str] = None) -> bool:
    """Update an existing lab result, scoped to user."""
    with get_db() as conn:
        conn.execute("""
            UPDATE lab_results
            SET date = ?,
                test_name = ?,
                numeric_value = ?,
                unit = ?,
                qualitative_result = ?,
                reference_range = ?,
                flag = ?,
                provider = ?,
                lab_facility = ?,
                notes = ?
            WHERE id = ? AND user_id = ?
        """, (date, test_name, numeric_value, unit, qualitative_result,
              reference_range, flag, provider, lab_facility, notes, lab_id, user_id))
    return True


def update_ana_result(user_id: int, ana_id: int, date: str,
                      titer: Optional[str] = None,
                      patterns: Optional[str] = None,
                      screen_result: Optional[str] = None,
                      provider: Optional[str] = None,
                      notes: Optional[str] = None) -> bool:
    """Update an existing ANA result, scoped to user."""
    with get_db() as conn:
        conn.execute("""
            UPDATE ana_results
            SET date = ?,
                titer_integer = ?,
                patterns = ?,
                screen_result = ?,
                provider = ?,
                notes = ?
            WHERE id = ? AND user_id = ?
        """, (date, titer, patterns, screen_result, provider, notes, ana_id, user_id))
    return True


def delete_ana_result(user_id: int, ana_id: int) -> bool:
    """Delete an ANA result, scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM ana_results WHERE id = ? AND user_id = ?",
                     (ana_id, user_id))
    return True


def update_clinical_event(user_id: int, event_id: int, date: str, event_type: str,
                          provider: Optional[str] = None,
                          facility: Optional[str] = None,
                          notes: Optional[str] = None) -> bool:
    """Update an existing clinical event, scoped to user."""
    with get_db() as conn:
        conn.execute("""
            UPDATE clinical_events
            SET date = ?,
                event_type = ?,
                provider = ?,
                facility = ?,
                notes = ?
            WHERE id = ? AND user_id = ?
        """, (date, event_type, provider, facility, notes, event_id, user_id))
    return True


def delete_clinical_event(user_id: int, event_id: int) -> bool:
    """Delete a clinical event, scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM clinical_events WHERE id = ? AND user_id = ?",
                     (event_id, user_id))
    return True


# ============================================================
# clinicians
# ============================================================

def add_clinician(user_id: int, data: dict) -> int:
    """Add a new clinician/provider for a user."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO clinicians (
                user_id, name, specialty, clinic_name, address,
                phone, email, network, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            data.get("name"),
            data.get("specialty"),
            data.get("clinic_name"),
            data.get("address"),
            data.get("phone"),
            data.get("email"),
            data.get("network"),
            data.get("notes"),
        ))
        return c.lastrowid


def get_all_clinicians(user_id: int) -> list[dict]:
    """Get all clinicians for a user ordered by name."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM clinicians
            WHERE user_id = ?
            ORDER BY name ASC
        """, (user_id,))
        return [dict(row) for row in c.fetchall()]


def update_clinician(user_id: int, clinician_id: int, name: str, specialty: str,
                     clinic_name: Optional[str] = None,
                     address: Optional[str] = None,
                     phone: Optional[str] = None,
                     email: Optional[str] = None,
                     network: Optional[str] = None,
                     notes: Optional[str] = None) -> bool:
    """Update an existing clinician, scoped to user."""
    with get_db() as conn:
        conn.execute("""
            UPDATE clinicians
            SET name = ?,
                specialty = ?,
                clinic_name = ?,
                address = ?,
                phone = ?,
                email = ?,
                network = ?,
                notes = ?
            WHERE id = ? AND user_id = ?
        """, (name, specialty, clinic_name, address, phone, email, network, notes,
              clinician_id, user_id))
    return True


def delete_clinician(user_id: int, clinician_id: int) -> bool:
    """Delete a clinician, scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM clinicians WHERE id = ? AND user_id = ?",
                     (clinician_id, user_id))
    return True


def update_medication(user_id: int, med_id: int, drug_name: str, start_date: str,
                     dose: Optional[float] = None,
                     unit: Optional[str] = None,
                     frequency: Optional[str] = None,
                     category: Optional[str] = None,
                     indication: Optional[str] = None,
                     end_date: Optional[str] = None,
                     notes: Optional[str] = None,
                     is_primary_intervention: bool = False,
                     is_secondary_intervention: bool = False) -> bool:
    """Update an existing medication, scoped to user."""
    with get_db() as conn:
        conn.execute("""
            UPDATE medications
            SET drug_name = ?,
                dose = ?,
                unit = ?,
                frequency = ?,
                category = ?,
                indication = ?,
                start_date = ?,
                end_date = ?,
                notes = ?,
                is_primary_intervention = ?,
                is_secondary_intervention = ?
            WHERE id = ? AND user_id = ?
        """, (drug_name, dose, unit, frequency, category, indication,
              start_date, end_date, notes,
              1 if is_primary_intervention else 0,
              1 if is_secondary_intervention else 0,
              med_id, user_id))
    return True


def delete_medication(user_id: int, med_id: int) -> bool:
    """Delete a medication, scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM medications WHERE id = ? AND user_id = ?",
                     (med_id, user_id))
    return True

def get_all_pending_doses_with_ntfy(window_start: str, window_end: str) -> list:
    """Return pending doses across ALL users, joined with each user's ntfy settings.
    Used by the scheduler for multi-user notifications."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT sd.*, m.drug_name, up.ntfy_topic, up.ntfy_server
               FROM scheduled_doses sd
               JOIN medications m ON m.id = sd.medication_id
               LEFT JOIN user_preferences up ON up.user_id = sd.user_id
               WHERE sd.scheduled_datetime >= ?
                 AND sd.scheduled_datetime < ?
                 AND sd.notified = 0
                 AND sd.taken = 0
               ORDER BY sd.scheduled_datetime""",
            (window_start, window_end)
        ).fetchall()
    return [dict(r) for r in rows]


def close_all_connections():
    """Close any open database connections."""
    pass


def insert_uv_sensor_rows(rows: list[dict]) -> int:
    """Bulk insert wearable UV samples and event marks. Returns rows accepted.

    Uses INSERT OR IGNORE keyed on (user_id, boot_id, ms_since_boot) so a
    re-sync of overlapping data is idempotent.
    """
    if not rows:
        return 0
    accepted = 0
    with get_db() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO uv_sensor_readings
                   (user_id, boot_id, ms_since_boot, ts, ts_confidence,
                    uva, uvb, comp1, comp2, uv_index, batt_mv, event_label)
                   VALUES (:user_id, :boot_id, :ms_since_boot, :ts, :ts_confidence,
                           :uva, :uvb, :comp1, :comp2, :uv_index, :batt_mv, :event_label)""",
                r,
            )
            accepted += cur.rowcount
    return accepted


def get_recent_uv_sensor_readings(user_id: int, hours: int = 24) -> list[dict]:
    """Wearable samples + events for a user within the last N hours.

    Pass hours=0 to skip the time filter entirely (returns everything with
    non-NULL ts). Excludes rows where ts is NULL (boots that couldn't be
    wall-clock anchored). Ordered ts ASC.

    Stored ts is naive local time with a T separator (Pi runs Chicago tz).
    The cutoff must match both the format and the timezone — `datetime('now')`
    alone returns UTC with a space separator, which silently broke comparison
    for rows from the same date.
    """
    hours = int(hours)
    with get_db() as conn:
        if hours <= 0:
            rows = conn.execute(
                """SELECT ts, ts_confidence, uva, uvb, comp1, comp2, uv_index,
                          batt_mv, event_label, boot_id
                   FROM uv_sensor_readings
                   WHERE user_id = ?
                     AND ts IS NOT NULL
                   ORDER BY ts ASC""",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT ts, ts_confidence, uva, uvb, comp1, comp2, uv_index,
                           batt_mv, event_label, boot_id
                    FROM uv_sensor_readings
                    WHERE user_id = ?
                      AND ts IS NOT NULL
                      AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime', '-{hours} hours')
                    ORDER BY ts ASC""",
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


# Every per-user table that a full "download my data" export should include.
# uv_data is intentionally absent: it's location-keyed and shared across users
# at the same coordinates, not personal data. Tables here must have a user_id
# column. Keep this list in sync when new user-scoped tables are added.
_USER_EXPORT_TABLES = (
    "daily_observations", "lab_results", "ana_results", "clinical_events",
    "medications", "medication_events", "clinicians", "bc_history",
    "taper_schedules", "scheduled_doses", "user_preferences",
    "uv_sensor_readings", "health_sync_events",
)


def export_table_for_user(table: str, user_id: int) -> dict:
    """Dump every row of a user-scoped table for the data-export ZIP.

    Returns {"columns": [...], "rows": [list of dicts]}. Columns come from the
    cursor description so an empty table still yields a header row. The table
    name is interpolated into the query, so it's checked against an allowlist
    (_USER_EXPORT_TABLES) — never pass caller/user input directly.
    """
    if table not in _USER_EXPORT_TABLES:
        raise ValueError(f"not an exportable user table: {table!r}")
    with get_db() as conn:
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE user_id = ? ORDER BY rowid",  # noqa: S608 (allowlisted)
            (user_id,),
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(r) for r in cur.fetchall()]
    return {"columns": columns, "rows": rows}


# ============================================================
# Contraceptive history functions
# ============================================================

def get_bc_history(user_id: int) -> list[dict]:
    """All BC history records for a user ordered by start_date ASC."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM bc_history WHERE user_id = ? ORDER BY start_date ASC",
            (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_bc_regime(user_id: int, data: dict) -> int:  # type: ignore[return]
    """Insert a new BC record for a user. Returns new id."""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO bc_history (user_id, bc_type, name, start_date, end_date, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, data["bc_type"], data.get("name"), data["start_date"],
             data.get("end_date"), data.get("notes")),
        )
    return cursor.lastrowid


def update_bc_regime(user_id: int, bc_id: int, data: dict) -> None:
    """Update an existing BC record, scoped to user."""
    with get_db() as conn:
        conn.execute(
            """UPDATE bc_history
               SET bc_type=?, name=?, start_date=?, end_date=?, notes=?
               WHERE id=? AND user_id=?""",
            (data["bc_type"], data.get("name"), data["start_date"],
             data.get("end_date"), data.get("notes"), bc_id, user_id),
        )


def delete_bc_regime(user_id: int, bc_id: int) -> None:
    """Delete a BC record, scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM bc_history WHERE id = ? AND user_id = ?",
                     (bc_id, user_id))


# ============================================================
# Taper schedule and dose reminder functions
# ============================================================

def create_taper_schedule(user_id: int, medication_id: int, start_date: str) -> int:
    """Create a new taper schedule for a medication. Returns the new schedule ID."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO taper_schedules (user_id, medication_id, start_date, active) VALUES (?, ?, ?, 1)",
            (user_id, medication_id, start_date)
        )
        return cursor.lastrowid


def insert_scheduled_doses(user_id: int, doses: list) -> None:
    """Bulk insert scheduled dose rows for a user."""
    for d in doses:
        d["user_id"] = user_id
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO scheduled_doses
               (user_id, taper_schedule_id, medication_id, scheduled_datetime, dose_label, dose_amount, dose_unit)
               VALUES (:user_id, :taper_schedule_id, :medication_id, :scheduled_datetime, :dose_label, :dose_amount, :dose_unit)""",
            doses
        )


def get_pending_doses(user_id: int, window_start: str, window_end: str) -> list:
    """Return doses scheduled between window_start and window_end that haven't been notified or taken."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT sd.*, m.drug_name
               FROM scheduled_doses sd
               JOIN medications m ON m.id = sd.medication_id
               WHERE sd.user_id = ?
                 AND sd.scheduled_datetime >= ?
                 AND sd.scheduled_datetime < ?
                 AND sd.notified = 0
                 AND sd.taken = 0
               ORDER BY sd.scheduled_datetime""",
            (user_id, window_start, window_end)
        ).fetchall()
    return [dict(r) for r in rows]


def mark_dose_notified(dose_id: int) -> None:
    """Mark a dose as having had its notification sent."""
    with get_db() as conn:
        conn.execute("UPDATE scheduled_doses SET notified = 1 WHERE id = ?", (dose_id,))


def mark_dose_taken(dose_id: int, taken_at: str) -> None:
    """Mark a dose as taken."""
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduled_doses SET taken = 1, taken_at = ? WHERE id = ?",
            (taken_at, dose_id)
        )


def get_todays_doses(user_id: int, date_str: str) -> list:
    """Return all scheduled doses for a user on a given date, with taken status."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT sd.*, m.drug_name
               FROM scheduled_doses sd
               JOIN medications m ON m.id = sd.medication_id
               WHERE sd.user_id = ?
                 AND sd.scheduled_datetime LIKE ?
               ORDER BY sd.scheduled_datetime""",
            (user_id, date_str + "%")
        ).fetchall()
    return [dict(r) for r in rows]


def get_active_taper_for_medication(user_id: int, medication_id: int) -> Optional[dict]:
    """Return the active taper schedule for a user's medication, or None."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT ts.*, COUNT(sd.id) as dose_count
               FROM taper_schedules ts
               LEFT JOIN scheduled_doses sd ON sd.taper_schedule_id = ts.id
               WHERE ts.user_id = ? AND ts.medication_id = ? AND ts.active = 1
               GROUP BY ts.id
               ORDER BY ts.created_at DESC
               LIMIT 1""",
            (user_id, medication_id)
        ).fetchone()
    return dict(row) if row else None


def get_active_tapers_with_doses(user_id: int, date_str: str) -> list:
    """Return all active taper schedules with dose summary for a user on a date."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ts.id as schedule_id, ts.medication_id, ts.start_date,
                      m.drug_name,
                      COUNT(sd.id) as total_doses,
                      SUM(sd.taken) as taken_doses
               FROM taper_schedules ts
               JOIN medications m ON m.id = ts.medication_id
               LEFT JOIN scheduled_doses sd ON sd.taper_schedule_id = ts.id
                                            AND sd.scheduled_datetime LIKE ?
               WHERE ts.user_id = ? AND ts.active = 1
               GROUP BY ts.id""",
            (date_str + "%", user_id)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_taper_schedule(user_id: int, schedule_id: int) -> None:
    """Delete a taper schedule and all its doses (cascade), scoped to user."""
    with get_db() as conn:
        conn.execute("DELETE FROM taper_schedules WHERE id = ? AND user_id = ?",
                     (schedule_id, user_id))