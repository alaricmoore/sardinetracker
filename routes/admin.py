"""
Login and registration, settings, help pages, admin, delete-all-data,
and the favicon.
"""

import os
from flask import jsonify, render_template, request, redirect, url_for, send_from_directory
import bcrypt
from flask_login import login_user, logout_user, login_required, current_user
import db
from pathlib import Path

from urllib.parse import urlsplit

from appcore import CONFIG, User, app, csrf


def _safe_next(target):
    """The ?next= page to return to after login, or None if it isn't a page on
    this site.

    Following any URL at all would let a crafted link send someone to a
    lookalike page straight after a real login. So only a plain path is
    allowed: not an absolute URL ("https://evil.example"), not a
    scheme-relative one ("//evil.example"), no backslash ("/\\evil.example",
    which browsers read as "//"), and no control characters (browsers drop
    tabs and newlines, which can turn "/\t/evil.example" into "//evil.example").
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


@app.route('/favicon/<path:filename>')
def favicon_files(filename):
    """Serve favicon assets from images/favicon/."""
    return send_from_directory(os.path.join(app.root_path, 'images', 'favicon'), filename)


@app.route("/login", methods=["GET", "POST"])
@csrf.exempt
def login():
    """Username + password login."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user_dict = db.get_user_by_username(username)
        if user_dict and bcrypt.checkpw(password.encode('utf-8'),
                                         user_dict['password_hash'].encode('utf-8')):
            user = User(user_dict)
            remember = bool(request.form.get("remember"))
            login_user(user, remember=remember)
            return redirect(_safe_next(request.args.get('next')) or url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    """Self-registration with invite code."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    invite_code = CONFIG.get("registration_invite_code")
    if not invite_code:
        return "Registration is disabled.", 403

    error = None
    if request.method == "POST":
        code = request.form.get("invite_code", "").strip()
        username = request.form.get("username", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        # Validate
        if code != invite_code:
            error = "Invalid invite code."
        elif not display_name:
            error = "Display name is required."
        elif len(username) < 3 or " " in username:
            error = "Username must be at least 3 characters, no spaces."
        elif db.get_user_by_username(username):
            error = "That username is already taken."
        elif len(password) < 4:
            error = "Password must be at least 4 characters."
        elif password != confirm:
            error = "Passwords don't match."
        else:
            pw_hash = bcrypt.hashpw(password.encode('utf-8'),
                                     bcrypt.gensalt()).decode('utf-8')
            user_id = db.create_user(username, display_name, pw_hash)
            user_dict = db.get_user_by_id(user_id)
            login_user(User(user_dict))
            return redirect(url_for("settings", welcome=1))

    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

# ============================================================
# DELETE ALL DATA
# ============================================================

@app.route("/delete/all-data", methods=["POST"])
def delete_all_data():
    """
    Delete the current user's account and all their tracking data.
    Irreversible — gated by a typed confirmation in the UI.

    This used to delete the whole database file and recreate it empty, which on
    a shared instance wiped every account, not just the one asking.
    """
    import shutil
    import routes.clinical as clinical

    try:
        user_id = current_user.id
        logout_user()
        db.delete_user(user_id)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # The uploaded files, only once the records are gone: removing files first
    # would leave records pointing at missing files if the delete then failed.
    docs_dir = os.path.join(clinical.DOCUMENTS_DIR, f"user_{user_id}")
    try:
        shutil.rmtree(docs_dir)
    except FileNotFoundError:
        pass
    except OSError as e:
        app.logger.error("delete_all_data: records deleted but %s remains: %s", docs_dir, e)
        return jsonify({"success": False,
                        "error": "Your records were deleted, but the uploaded document "
                                 "files could not be removed. Check the server log."}), 500

    return jsonify({"success": True, "message": "All data deleted"}), 200


# ============================================================
# Settings
# ============================================================

@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Per-user settings page."""
    prefs = db.get_user_preferences(current_user.id) or {}
    saved = False
    pw_error = None

    if request.method == "POST":
        # Collect form data
        new_prefs = {
            'patient_name': request.form.get('patient_name', '').strip() or None,
            'patient_dob': request.form.get('patient_dob', '').strip() or None,
            'timezone': request.form.get('timezone', '').strip() or 'America/Chicago',
            'track_cycle': 1 if request.form.get('track_cycle') else 0,
            'primary_intervention_name': request.form.get('primary_intervention_name', '').strip() or None,
            'primary_intervention_date': request.form.get('primary_intervention_date', '').strip() or None,
            'ntfy_topic': request.form.get('ntfy_topic', '').strip() or None,
            'ntfy_server': request.form.get('ntfy_server', '').strip() or 'https://ntfy.sh',
        }

        # Daily reminder (hours since last log, None = disabled)
        reminder_val = request.form.get('reminder_hours', '').strip()
        if reminder_val == '':
            new_prefs['reminder_hours'] = None
        else:
            try:
                new_prefs['reminder_hours'] = int(reminder_val)
            except ValueError:
                new_prefs['reminder_hours'] = None

        # Numeric fields
        try:
            lat = request.form.get('location_lat', '').strip()
            new_prefs['location_lat'] = float(lat) if lat else None
        except ValueError:
            new_prefs['location_lat'] = prefs.get('location_lat')

        try:
            lon = request.form.get('location_lon', '').strip()
            new_prefs['location_lon'] = float(lon) if lon else None
        except ValueError:
            new_prefs['location_lon'] = prefs.get('location_lon')

        try:
            temp = request.form.get('temp_baseline_f', '').strip()
            new_prefs['temp_baseline_f'] = float(temp) if temp else 97.4
        except ValueError:
            new_prefs['temp_baseline_f'] = prefs.get('temp_baseline_f', 97.4)

        # Save preferences
        db.upsert_user_preferences(current_user.id, new_prefs)

        # Handle password change
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')
        if new_pw:
            if new_pw != confirm_pw:
                pw_error = "Passwords don't match."
            elif len(new_pw) < 4:
                pw_error = "Password must be at least 4 characters."
            else:
                pw_hash = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                db.update_user_password(current_user.id, pw_hash)

        if not pw_error:
            saved = True

        # Refresh prefs after save
        prefs = db.get_user_preferences(current_user.id) or {}

        # Clear the cached prefs so inject_globals picks up changes
        from flask import g
        if hasattr(g, '_user_prefs'):
            del g._user_prefs

    welcome = request.args.get("welcome") == "1" and request.method == "GET"
    return render_template("settings.html", prefs=prefs, saved=saved, pw_error=pw_error, welcome=welcome)


# ============================================================
# Help
# ============================================================

@app.route("/help")
def help_page():
    """Searchable help page. The content is help.md, the same file published at
    sardinetracker.com/docs/help, so the in-app help and the website can't drift apart."""
    help_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "help.md")
    try:
        with open(help_path, "r") as f:
            help_content = f.read()
    except FileNotFoundError:
        help_content = "help.md not found."
    return render_template("help.html", content=help_content)


@app.route("/readme")
@login_required
def readme_page():
    """Render the project README as a styled page (no nav link)."""
    readme_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md")
    try:
        with open(readme_path, "r") as f:
            readme_content = f.read()
    except FileNotFoundError:
        readme_content = "README.md not found."
    return render_template("readme.html", content=readme_content)


@app.route("/model/docs")
@login_required
def model_explainer():
    """Render MODEL.md as a styled page."""
    model_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "MODEL.md")
    try:
        with open(model_path, "r") as f:
            model_content = f.read()
    except FileNotFoundError:
        model_content = "MODEL.md not found."
    return render_template("readme.html", content=model_content)


@app.route("/remote-access")
@login_required
def remote_access_page():
    """Render REMOTE_ACCESS.md as a styled page (no nav link)."""
    ra_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "REMOTE_ACCESS.md")
    try:
        with open(ra_path, "r") as f:
            ra_content = f.read()
    except FileNotFoundError:
        ra_content = "REMOTE_ACCESS.md not found."
    return render_template("remote_access.html", content=ra_content)


# ============================================================
# Admin
# ============================================================

@app.route("/admin", methods=["GET"])
def admin_panel():
    """Admin panel for managing users."""
    if not current_user.is_admin:
        return redirect(url_for("index"))
    users = db.get_all_users()
    return render_template("admin.html", users=users)


@app.route("/admin/reset-password/<int:user_id>", methods=["POST"])
def admin_reset_password(user_id):
    """Reset a user's password (admin only)."""
    if not current_user.is_admin:
        return redirect(url_for("index"))
    new_pw = request.form.get("new_password", "")
    if len(new_pw) < 4:
        return redirect(url_for("admin_panel"))
    pw_hash = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db.update_user_password(user_id, pw_hash)
    return redirect(url_for("admin_panel"))


@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
def admin_delete_user(user_id):
    """Delete a user and all their data (admin only)."""
    if not current_user.is_admin:
        return redirect(url_for("index"))
    # Prevent self-deletion
    if user_id == current_user.id:
        return redirect(url_for("admin_panel"))
    db.delete_user(user_id)
    return redirect(url_for("admin_panel"))
