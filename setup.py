"""
biotracking setup.py
--------------------
Run once to initialize the database and create your local config file.
Your config file will be created at config.json and is gitignored -
it will never be committed to GitHub.

Usage:
    python setup.py
"""

import json
import os
import secrets
import sqlite3
import sys


CONFIG_FILE = "config.json"
DB_FILE = "biotracking.db"


def prompt(message, default=None):
    """Prompt the user for input with an optional default."""
    if default:
        result = input(f"{message} [{default}]: ").strip()
        return result if result else default
    result = input(f"{message}: ").strip()
    return result


def load_existing_config() -> dict:
    """Load existing config.json if it exists, so re-runs preserve current values."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def create_config():
    """Walk the user through creating their local config file."""
    print("\n--- sardinetracker first-time setup ---")
    print("This config file is gitignored and stays on your machine only.")
    print("Re-running setup will not erase existing values unless you change them.\n")

    existing = load_existing_config()
    config = {}

    config["patient_name"] = prompt("Your name or identifier (used in exports and reports)",
                                    default=existing.get("patient_name"))
    config["patient_dob"] = prompt("Date of birth (YYYY-MM-DD, used in clinic exports)",
                                   default=existing.get("patient_dob"))

    print("\nLocation is used only to pull UV index data from a weather API.")
    print("It is stored locally in config.json and never sent with any health data.\n")
    config["location_lat"] = float(prompt("Latitude (e.g. 35.4676 for Oklahoma City)",
                                          default=existing.get("location_lat")))
    config["location_lon"] = float(prompt("Longitude (e.g. -97.5164 for Oklahoma City)",
                                          default=existing.get("location_lon")))
    config["timezone"] = prompt("Timezone", default=existing.get("timezone", "America/Chicago"))

    print("\nBaseline values help calculate meaningful deltas over time.")
    config["temp_baseline_f"] = float(
        prompt("Your baseline wrist temperature in °F (e.g. 97.4)",
               default=existing.get("temp_baseline_f"))
    )

    print("\nPrimary intervention tracking (optional):")
    print("If you're on a disease-modifying medication (e.g., hydroxychloroquine,")
    print("methotrexate, rituximab), you can track its start date to measure")
    print("pre/post effects on HRV and symptoms. You can skip this and add it later.")

    existing_intervention = existing.get("primary_intervention") or {}
    track_intervention = prompt("Track a primary intervention? (y/n)",
                                default="y" if existing_intervention else "n").lower()

    if track_intervention == "y":
        intervention_name = prompt("Medication name (e.g., hydroxychloroquine)",
                                   default=existing_intervention.get("name"))
        intervention_date = prompt("Start date (YYYY-MM-DD)",
                                   default=existing_intervention.get("start_date"))
        config["primary_intervention"] = {
            "name": intervention_name,
            "start_date": intervention_date
        }
    else:
        config["primary_intervention"] = None

    print("\nOptional: menstrual cycle tracking")
    print("Adds a cycle card to daily entries and a month-grid calendar at /cycle.")
    print("Includes BBT-based ovulation detection and flare/intervention correlation.")
    track_cycle = prompt("Track menstrual cycle? (y/n)",
                         default="y" if existing.get("track_cycle") else "n").lower()
    config["track_cycle"] = track_cycle == "y"

    print("\nOptional: push notifications via ntfy.sh")
    print("Used for medication dose reminders and flare risk alerts.")
    print("Create a free topic at https://ntfy.sh — subscribe to it in the ntfy app.\n")
    ntfy_topic = prompt("ntfy topic name (leave blank to skip)",
                        default=existing.get("ntfy_topic", ""))
    config["ntfy_topic"] = ntfy_topic if ntfy_topic else existing.get("ntfy_topic", "")
    if config["ntfy_topic"]:
        config["ntfy_server"] = prompt("ntfy server URL",
                                       default=existing.get("ntfy_server", "https://ntfy.sh"))

    print("\nOptional: Visual Crossing API key")
    print("Used to backfill historical UV data beyond Open-Meteo's 16-day limit.")
    print("Free tier available at https://www.visualcrossing.com/weather-api\n")
    vc_key = prompt("Visual Crossing API key (leave blank to skip)",
                    default=existing.get("visual_crossing_api_key", ""))
    config["visual_crossing_api_key"] = vc_key if vc_key else existing.get("visual_crossing_api_key", "")

    config["app_version"] = "2.0.0"
    config["debug"] = existing.get("debug", False)
    # Preserve existing secret_key — regenerating it invalidates active sessions
    config["secret_key"] = existing.get("secret_key") or secrets.token_hex(32)
    # API token for iOS Shortcut / programmatic health-sync endpoint
    config["api_token"] = existing.get("api_token") or secrets.token_hex(32)
    # Preserve passcode if set
    if existing.get("passcode"):
        config["passcode"] = existing["passcode"]

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nConfig saved to {CONFIG_FILE} (gitignored - stays local)")
    if not existing.get("api_token"):
        print(f"\nAPI token generated for health-sync endpoint:")
        print(f"  {config['api_token']}")
        print("  (copy this into your iOS Shortcut Authorization header)")
    print("\nOptional: add \"passcode\": \"yourpin\" to config.json to require")
    print("a login passcode to access the app (useful on shared networks).")
    return config


def create_database():
    """Create the SQLite database and all tables."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Enable WAL mode for better concurrent read performance
    c.execute("PRAGMA journal_mode=WAL")

    # --------------------------------------------------------
    # users
    # Authentication and identity for multi-user support
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            display_name  TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # --------------------------------------------------------
    # daily_observations
    # Core symptom and biometric data, one row per user per day
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_observations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL REFERENCES users(id),
            date                TEXT NOT NULL,      -- YYYY-MM-DD
            steps               INTEGER,
            hours_slept         REAL,
            hrv                 REAL,
            hrv_rmssd           REAL,              -- RMSSD (parasympathetic HRV, ms)
            resting_heart_rate  REAL,              -- resting HR in bpm
            spo2                REAL,              -- blood oxygen %
            respiratory_rate    REAL,              -- breaths per minute
            basal_temp_delta    REAL,              -- deviation from personal baseline
            sun_exposure_min    INTEGER,           -- minutes, from Apple Health or manual
            pain_scale          REAL,              -- 0-10
            fatigue_scale       REAL,              -- 0-10
            emotional_state     REAL,              -- 0-10
            emotional_notes     TEXT,

            -- Neurological
            neurological        INTEGER DEFAULT 0, -- boolean
            neuro_notes         TEXT,

            -- Cognitive
            cognitive           INTEGER DEFAULT 0,
            cognitive_notes     TEXT,

            -- Musculature
            musculature         INTEGER DEFAULT 0,
            musculature_notes   TEXT,

            -- Migraine / headache
            migraine            INTEGER DEFAULT 0,
            migraine_notes      TEXT,

            -- Air hunger / chest discomfort
            air_hunger          INTEGER DEFAULT 0,
            air_hunger_notes    TEXT,

            -- Dermatological
            dermatological      INTEGER DEFAULT 0,
            derm_notes          TEXT,

            -- Word loss / stuttering
            word_loss           INTEGER DEFAULT 0,

            -- Flare tracking
            strike_physical     INTEGER DEFAULT 0, -- boolean
            strike_environmental INTEGER DEFAULT 0,
            flare_occurred      INTEGER DEFAULT 0,
            flare_severity      TEXT,

            -- Catch-all
            notes               TEXT,

            -- UV context
            stayed_indoors      INTEGER DEFAULT 0,
            uv_protection_level TEXT,

            UNIQUE(user_id, date)
        )
    """)
    
    # --------------------------------------------------------
    # health_sync_events
    # Audit trail for incoming POSTs to /api/health-sync — used by the
    # /daily page's "recent HealthKit syncs" panel for trust/visibility.
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS health_sync_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            posted_at       TEXT NOT NULL,    -- ISO timestamp when the POST landed
            metric_date     TEXT NOT NULL,    -- the YYYY-MM-DD the sync was for
            fields_updated  TEXT NOT NULL,    -- JSON array of field names
            payload_json    TEXT NOT NULL     -- JSON object of {field: value}
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_health_sync_events_user_posted
        ON health_sync_events(user_id, posted_at DESC)
    """)

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN rheumatic INTEGER DEFAULT 0")
    except:
        pass  # Column already exists
    
    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN rheumatic_notes TEXT")
    except:
        pass  # Column already exists
    
    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN word_loss_notes TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN period_flow TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN cramping TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN cycle_notes TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN pulmonary INTEGER DEFAULT 0")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN pulmonary_notes TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN mucosal INTEGER DEFAULT 0")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN mucosal_notes TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN gastro INTEGER DEFAULT 0")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN gastro_notes TEXT")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN resting_heart_rate REAL")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN hrv_rmssd REAL")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN spo2 REAL")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE daily_observations ADD COLUMN respiratory_rate REAL")
    except:
        pass  # Column already exists

    # --------------------------------------------------------
    # uv_data
    # UV index by location+date, pulled from API or entered manually
    # Shared across users at the same location
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS uv_data (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            location_key TEXT NOT NULL DEFAULT 'default',  -- "lat,lon" rounded to 2 decimals
            date         TEXT NOT NULL,   -- YYYY-MM-DD
            uv_morning   REAL,
            uv_noon      REAL,
            uv_evening   REAL,
            source          TEXT DEFAULT 'api',
            cloud_cover_pct  REAL,
            temperature_high REAL,
            weather_summary  TEXT,
            UNIQUE(location_key, date)
        )
    """)

    # --------------------------------------------------------
    # lab_results
    # General labs - numeric or qualitative
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS lab_results (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER REFERENCES users(id),
            date                TEXT NOT NULL,      -- YYYY-MM-DD
            test_name           TEXT NOT NULL,
            numeric_value       REAL,               -- nullable
            unit                TEXT,               -- nullable
            qualitative_result  TEXT,               -- 'positive', 'negative', etc.
            reference_range     TEXT,               -- e.g. '0-20 IU/mL'
            flag                TEXT,               -- 'high', 'low', 'abnormal', 'normal'
            provider            TEXT,
            lab_facility        TEXT,
            notes               TEXT
        )
    """)

    # --------------------------------------------------------
    # ana_results
    # ANA gets its own table due to titer + pattern complexity
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS ana_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER REFERENCES users(id),
            date            TEXT NOT NULL,          -- YYYY-MM-DD
            titer_integer   INTEGER,                -- stored as int: 40, 80, 160
            screen_result   TEXT,                   -- 'positive' or 'negative'
            patterns        TEXT,                   -- JSON array: ["AC-4","AC-29"]
            provider        TEXT,
            notes           TEXT
        )
    """)

    # --------------------------------------------------------
    # clinical_events
    # Encounters, biopsies, injections, procedures
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS clinical_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER REFERENCES users(id),
            date            TEXT NOT NULL,          -- YYYY-MM-DD
            event_type      TEXT NOT NULL,          -- 'encounter', 'biopsy', 'injection', 'procedure', 'other'
            provider        TEXT,
            facility        TEXT,
            notes           TEXT,
            follow_up_date  TEXT                    -- nullable YYYY-MM-DD
        )
    """)

    # --------------------------------------------------------
    # medications
    # Prescriptions, supplements, OTCs - one row per course
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS medications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            drug_name   TEXT NOT NULL,
            dose        REAL,
            unit        TEXT,                       -- 'mg', 'mcg', 'IU', etc.
            frequency   TEXT,                       -- 'daily', 'twice daily', 'as needed', etc.
            route       TEXT,                       -- 'oral', 'topical', 'nasal', 'IV', etc.
            category    TEXT,                       -- 'prescription', 'supplement', 'OTC'
            indication  TEXT,                       -- reason / purpose
            start_date  TEXT NOT NULL,              -- YYYY-MM-DD
            end_date    TEXT,                       -- nullable, null = currently active
            notes       TEXT
        )
    """)
    
        # Add intervention tracking columns (migration)
    try:
        c.execute("ALTER TABLE medications ADD COLUMN is_primary_intervention INTEGER DEFAULT 0")
    except:
        pass  # Column already exists

    try:
        c.execute("ALTER TABLE medications ADD COLUMN is_secondary_intervention INTEGER DEFAULT 0")
    except:
        pass  # Column already exists

    # --------------------------------------------------------
    # medication_events
    # Dated observations about a medication: side effects, rebound,
    # efficacy change, dose change, or general note.
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS medication_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id),
            medication_id  INTEGER NOT NULL,
            event_date     TEXT NOT NULL,
            event_type     TEXT NOT NULL,    -- side_effect | rebound | efficacy_change | dose_change | note
            severity       INTEGER,           -- 0-10 for side_effect; null otherwise
            note           TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (medication_id) REFERENCES medications(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_med_events_med ON medication_events(medication_id, event_date)")

    # --------------------------------------------------------
    # taper_schedules
    # One row per configured taper course linked to a medication
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS taper_schedules (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER REFERENCES users(id),
            medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
            start_date   TEXT NOT NULL,   -- YYYY-MM-DD
            active       INTEGER DEFAULT 1,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # --------------------------------------------------------
    # scheduled_doses
    # Individual dose events for a taper schedule
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_doses (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER REFERENCES users(id),
            taper_schedule_id   INTEGER NOT NULL REFERENCES taper_schedules(id) ON DELETE CASCADE,
            medication_id       INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
            scheduled_datetime  TEXT NOT NULL,  -- 'YYYY-MM-DD HH:MM'
            dose_label          TEXT NOT NULL,  -- e.g. 'Day 1 - Morning (2 tablets)'
            dose_amount         REAL,
            dose_unit           TEXT,
            taken               INTEGER DEFAULT 0,
            taken_at            TEXT,           -- ISO datetime when marked taken
            notified            INTEGER DEFAULT 0
        )
    """)

    # --------------------------------------------------------
    # FTS5 virtual table for keyword search across note fields
    # --------------------------------------------------------
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_search
        USING fts5(
            date,
            source_table,
            notes_text,
            content=''
        )
    """)
    
    # --------------------------------------------------------
    # Clinician information
    # --------------------------------------------------------
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS clinicians (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER REFERENCES users(id),
            name            TEXT NOT NULL,
            specialty       TEXT NOT NULL,
            clinic_name     TEXT,
            address         TEXT,
            phone           TEXT,
            email           TEXT,
            network         TEXT,
            notes           TEXT
        )
    """)

    # --------------------------------------------------------
    # bc_history
    # Contraceptive history — kept separate from medications so it
    # stays out of clinical-record exports and supports typed analytics
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS bc_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            bc_type     TEXT NOT NULL,   -- see BC_TYPE_LABELS in app.py
            name        TEXT,            -- brand / formulation (optional)
            start_date  TEXT NOT NULL,   -- YYYY-MM-DD
            end_date    TEXT,            -- nullable, null = currently active
            notes       TEXT
        )
    """)

    # --------------------------------------------------------
    # user_preferences
    # Per-user settings (patient info, location, notifications, etc.)
    # One row per user, keyed by user_id
    # --------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id                   INTEGER PRIMARY KEY REFERENCES users(id),
            patient_name              TEXT,
            patient_dob               TEXT,
            location_lat              REAL,
            location_lon              REAL,
            timezone                  TEXT DEFAULT 'America/Chicago',
            temp_baseline_f           REAL DEFAULT 97.4,
            track_cycle               INTEGER DEFAULT 0,
            ntfy_topic                TEXT,
            ntfy_server               TEXT DEFAULT 'https://ntfy.sh',
            custom_weights            TEXT,
            primary_intervention_name TEXT,
            primary_intervention_date TEXT,
            last_flare_alert_date     TEXT,
            last_uv_alert_date        TEXT,
            reminder_hours            INTEGER,
            last_logged_at            TEXT,
            last_reminder_date        TEXT,
            last_period_nudge_date    TEXT,
            steps_baseline            INTEGER
        )
    """)

    # Migration: add reminder columns if missing (existing DBs)
    for col, coltype in [("reminder_hours", "INTEGER"), ("last_logged_at", "TEXT"),
                         ("last_reminder_date", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE user_preferences ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # Column already exists
    # Drop old column name if present (SQLite ignores, harmless)
    # daily_reminder_hour is superseded by reminder_hours

    conn.commit()
    conn.close()
    print(f"Database created at {DB_FILE} (gitignored - stays local)")


def verify_setup():
    """Quick sanity check that everything was created correctly."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in c.fetchall()]
    conn.close()

    expected = [
        "users",
        "user_preferences",
        "daily_observations",
        "uv_data",
        "lab_results",
        "ana_results",
        "clinical_events",
        "medications",
        "taper_schedules",
        "scheduled_doses",
        "notes_search"
    ]

    missing = [t for t in expected if t not in tables]
    if missing:
        print(f"\nWarning: missing tables: {missing}")
        return False

    print(f"\nVerification passed. Tables created: {', '.join(expected)}")
    return True


def main():
    print("sardinetracker setup")
    print("==================")

    # Check for existing setup
    if os.path.exists(CONFIG_FILE) and os.path.exists(DB_FILE):
        confirm = input(
            "\nSetup files already exist. Re-run setup? "
            "This will NOT delete existing data. (y/n): "
        ).strip().lower()
        if confirm != "y":
            print("Setup cancelled.")
            sys.exit(0)

    create_config()
    create_database()
    verify_setup()

    print("\n--- Setup complete ---")
    print("Next steps:")
    print("  1. Activate your virtual environment: source .venv/bin/activate")
    print("  2. Install dependencies: pip install -r requirements.txt")
    print("  3. Run the app: python app.py")
    print("  4. Open your browser to: http://localhost:5000")
    print("\nTo access from your phone, connect to the same wifi network")
    print("and visit: http://<your-mac-ip>:5000")


if __name__ == "__main__":
    main()