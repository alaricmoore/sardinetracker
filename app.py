"""
biotracking app.py
------------------
The entry point. Importing this module assembles the whole app:

    appcore.py      the Flask app, config, migrations, CSRF, login, request hooks
    flaremodel.py   shared scoring layer used by pages, the API and reminders
    reminders.py    ntfy notifications and scheduled jobs (starts the scheduler)
    routes/         the pages and API, one module per area of the app

Rules that hold across all of them: data access goes through db.py, and UV
fetching goes through uv_fetcher.py.

Run with:
    python app.py

Access locally:    http://localhost:5000
Access from phone: http://<your-mac-ip>:5000
"""

# Names that scripts outside the app reach through `app` (see analysis_cycle_vs_hrv.py).
from appcore import CONFIG, get_user_prefs  # noqa: F401
from flaremodel import DEFAULT_WEIGHTS, _inject_cycle_phase, _inject_scoring_context, calculate_flare_prime_score, calculate_flare_score_with_weights  # noqa: F401
from flask_login import current_user  # noqa: F401
from appcore import app

# Importing a module registers its URLs on the app. reminders starts the scheduler.
import flaremodel  # noqa: F401
import reminders  # noqa: F401
import routes.daily  # noqa: F401
import routes.dashboard  # noqa: F401
import routes.interventions  # noqa: F401
import routes.clinical  # noqa: F401
import routes.portal  # noqa: F401
import routes.forecast  # noqa: F401
import routes.reports  # noqa: F401
import routes.api  # noqa: F401
import routes.admin  # noqa: F401


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    print("\nsardinetracker")
    print("==============")
    print(f"Patient: {CONFIG.get('patient_name', 'not set')}")
    print(f"Starting server...")
    print(f"\nLocal:  http://localhost:5000")
    print(f"Phone:  connect to same wifi, visit http://<your-ip>:5000\n")

    app.run(
        host="0.0.0.0",   # accessible from phone on same network
        port=5000,
        debug=CONFIG.get('debug', False),
    )
    
