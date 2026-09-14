"""
The Flask app itself: config, database migrations, secret key and CSRF,
login, and the request hooks every page goes through.
"""

import json
import os
from datetime import date
from flask import Flask, request, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, current_user
import db
from flask_wtf.csrf import CSRFProtect


app = Flask(__name__)

# Apply any pending schema migrations. Idempotent — safe to call every startup.
# Prints a note only when something actually changed so logs stay quiet on
# no-op runs.
_migrations_applied = db.run_migrations()
if _migrations_applied:
    print(f"[db] applied {_migrations_applied} schema migration(s) at startup")


# All writable state (config.json, the database, custom weights, uploaded
# documents) lives under DATA_DIR. Defaults to the repo directory — the
# classic self-hosted layout. Embedded platforms (the Android local app)
# set SARDINE_DATA_DIR to app-private storage before importing this module.
DATA_DIR = os.environ.get("SARDINE_DATA_DIR", os.path.dirname(__file__))


# ============================================================
# Config loading
# ============================================================

def load_config() -> dict:
    """Load local config. Exits cleanly if setup hasn't been run."""
    config_path = os.path.join(DATA_DIR, "config.json")
    if not os.path.exists(config_path):
        print("ERROR: config.json not found. Run setup.py first.")
        raise SystemExit(1)
    with open(config_path) as f:
        return json.load(f)


CONFIG = load_config()


# ============================================================
# Auto-migrate: add any missing columns to existing databases
# ============================================================
def _auto_migrate():
    """Add columns that may be missing from older databases.
    Each ALTER TABLE is wrapped in try/except so it's safe to run repeatedly."""
    import sqlite3
    db_path = db.DB_FILE
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    migrations = [
        ("user_preferences", "reminder_hours", "INTEGER"),
        ("user_preferences", "last_logged_at", "TEXT"),
        ("user_preferences", "last_reminder_date", "TEXT"),
        ("daily_observations", "pulmonary", "INTEGER DEFAULT 0"),
        ("daily_observations", "pulmonary_notes", "TEXT"),
        ("daily_observations", "mucosal", "INTEGER DEFAULT 0"),
        ("daily_observations", "mucosal_notes", "TEXT"),
        ("daily_observations", "gastro", "INTEGER DEFAULT 0"),
        ("daily_observations", "gastro_notes", "TEXT"),
        ("daily_observations", "stayed_indoors", "INTEGER DEFAULT 0"),
        ("daily_observations", "uv_protection_level", "TEXT"),
        ("uv_data", "cloud_cover_pct", "REAL"),
        ("uv_data", "temperature_high", "REAL"),
        ("uv_data", "weather_summary", "TEXT"),
        ("user_preferences", "last_period_nudge_date", "TEXT"),
        ("daily_observations", "flare_severity", "TEXT"),
        ("user_preferences", "steps_baseline", "INTEGER"),
    ]
    for table, col, coltype in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

_auto_migrate()


# ============================================================
# Security: SECRET_KEY, CSRF, optional passcode
# ============================================================

_secret = CONFIG.get('secret_key')
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
    print("[WARNING] No secret_key in config.json. Generated a temporary one — "
          "sessions will reset on every restart. Run setup.py to persist it.")
app.secret_key = _secret

csrf = CSRFProtect(app)

# ============================================================
# Flask-Login setup
# ============================================================

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = None  # suppress default flash message


class User(UserMixin):
    """Wraps a user dict from the database for Flask-Login."""
    def __init__(self, user_dict):
        self._data = user_dict

    def get_id(self):
        return str(self._data['id'])

    @property
    def id(self):
        return self._data['id']

    @property
    def username(self):
        return self._data['username']

    @property
    def display_name(self):
        return self._data['display_name']

    @property
    def is_admin(self):
        return bool(self._data.get('is_admin'))


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID for Flask-Login session management."""
    user_dict = db.get_user_by_id(int(user_id))
    if user_dict:
        return User(user_dict)
    return None


@app.before_request
def require_login():
    """Redirect unauthenticated users to login page."""
    if request.endpoint in ('login', 'register', 'static', 'favicon_files', 'api_health_sync', 'api_flare_status', 'api_uv_ingest', 'portal_view', 'portal_section', 'portal_document'):
        return
    if not current_user.is_authenticated:
        # Single-user mode (a server that belongs to exactly one person, e.g.
        # the phone-local app): sign in as the sole account automatically.
        # Deliberately refuses to guess when more than one account exists.
        if CONFIG.get("single_user_mode"):
            sole = db.get_sole_user()
            if sole:
                login_user(User(sole), remember=True)
                return
        return redirect(url_for('login'))


def get_user_prefs() -> dict:
    """Get current user's preferences, cached per-request via Flask g.
    Returns empty dict for unauthenticated users or users with no prefs yet.
    """
    from flask import g
    if not hasattr(g, '_user_prefs'):
        if current_user.is_authenticated:
            g._user_prefs = db.get_user_preferences(current_user.id) or {}
        else:
            g._user_prefs = {}
    return g._user_prefs


def get_location_key() -> str:
    """Get the current user's location key for UV data lookups."""
    prefs = get_user_prefs()
    lat = prefs.get('location_lat') or CONFIG.get('location_lat')
    lon = prefs.get('location_lon') or CONFIG.get('location_lon')
    if lat and lon:
        return db.make_location_key(float(lat), float(lon))
    return 'default'


def uid() -> int:
    """Shorthand for current_user.id — used throughout routes."""
    return current_user.id


# ============================================================
# Template context - available in every template
# ============================================================

@app.context_processor
def inject_globals():
    """Inject values available in every template."""
    prefs = get_user_prefs()
    return {
        "patient_name": prefs.get("patient_name") or CONFIG.get("patient_name", ""),
        "patient_dob": prefs.get("patient_dob") or CONFIG.get("patient_dob", ""),
        "today": date.today().isoformat(),
        "app_version": CONFIG.get("app_version", "2.0.0"),
        "track_cycle": bool(prefs.get("track_cycle")) if prefs.get("track_cycle") is not None else False,
        "config": CONFIG,
        "current_user": current_user,
    }
