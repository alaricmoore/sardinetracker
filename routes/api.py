"""
Token-authenticated JSON API: health sync, UV sensor ingest, flare status,
backup export and restore, and chart data.
"""

from scoring import UNCALIBRATED_RMSSD_BOUNDS, calibrate_rmssd_bounds, rmssd_is_implausible
import json
import os
from datetime import date, datetime, timedelta
from flask import jsonify, request, send_file
from flask_login import login_required
import db
import zipfile
from typing import Dict

from appcore import CONFIG, DATA_DIR, app, csrf, get_location_key, uid
from flaremodel import CUSTOM_WEIGHTS_PATH, _inject_scoring_context, calculate_flare_prime_score, get_contributing_factors, get_current_weights, get_risk_level
from routes.reports import _build_backup_zip


@app.route("/api/backup/export")
@csrf.exempt
def api_backup_export():
    """Token-authenticated backup export (no session), for programmatic hosts
    like the Android local app. Auth mirrors /api/health-sync."""
    token = CONFIG.get("api_token")
    if not token:
        return jsonify({"error": "api_token not configured"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != token:
        return jsonify({"error": "unauthorized"}), 401

    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        sole = db.get_sole_user()
        if not sole:
            return jsonify({"error": "user_id required on multi-user servers"}), 400
        user_id = sole["id"]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        _build_backup_zip(user_id),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'biotracking_backup_{timestamp}.zip'
    )


@app.route("/api/backup/restore", methods=["POST"])
@csrf.exempt
def api_backup_restore():
    """Full-replace restore from a backup zip. Deliberately refuses to run on
    multi-user servers — replacing the shared database is not any one user's
    call; use import_backup.py there. Auth mirrors /api/health-sync.

    config.json from the backup is merged, not swapped: the running install
    keeps its own secret_key / api_token / single_user_mode (swapping those
    live would break the session and the sync bridge), takes the rest, and
    the merged values apply on next restart (CONFIG is read at startup).
    """
    token = CONFIG.get("api_token")
    if not token:
        return jsonify({"error": "api_token not configured"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != token:
        return jsonify({"error": "unauthorized"}), 401
    if not CONFIG.get("single_user_mode"):
        return jsonify({"error": "restore only runs on single-user servers"}), 403

    from io import BytesIO
    if "backup" in request.files:
        payload = request.files["backup"].read()
    else:
        payload = request.get_data()
    try:
        zf = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile:
        return jsonify({"error": "not a zip file"}), 400
    if "biotracking.db" not in zf.namelist():
        return jsonify({"error": "backup contains no biotracking.db"}), 400

    import sqlite3 as _sqlite3
    tmp_path = os.path.join(DATA_DIR, ".restore_incoming.db")
    with open(tmp_path, "wb") as f:
        f.write(zf.read("biotracking.db"))
    try:
        src = _sqlite3.connect(tmp_path)
        try:
            src.execute("SELECT COUNT(*) FROM users").fetchone()
        except _sqlite3.DatabaseError:
            return jsonify({"error": "backup database is invalid or corrupt"}), 400

        # Online replace: SQLite's backup API takes proper locks, so the
        # running server's other connections stay consistent throughout.
        dest = _sqlite3.connect(db.DB_FILE)
        with dest:
            src.backup(dest)
        dest.close()
        src.close()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # The backup may predate the current schema.
    migrations = db.run_migrations()

    restored_files = 0
    for name in zf.namelist():
        target = None
        if name == "config/custom_weights.json":
            target = CUSTOM_WEIGHTS_PATH
        elif name.startswith("documents/"):
            target = os.path.join(DATA_DIR, name)
        if not target:
            continue
        # Refuse anything that escapes DATA_DIR (zip path traversal)
        real = os.path.realpath(target)
        if not real.startswith(os.path.realpath(DATA_DIR) + os.sep):
            continue
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "wb") as f:
            f.write(zf.read(name))
        restored_files += 1

    merged_config = False
    if "config.json" in zf.namelist():
        try:
            incoming = json.loads(zf.read("config.json"))
            config_path = os.path.join(DATA_DIR, "config.json")
            with open(config_path) as f:
                current = json.load(f)
            for key in ("secret_key", "api_token", "single_user_mode"):
                incoming[key] = current.get(key)
            with open(config_path, "w") as f:
                json.dump(incoming, f, indent=2)
            merged_config = True
        except (ValueError, OSError) as e:
            print(f"[restore] config merge skipped: {e}")

    return jsonify({
        "ok": True,
        "migrations_applied": migrations,
        "aux_files_restored": restored_files,
        "config_merged": merged_config,
    })


# ============================================================
# Health-sync API (iOS Shortcut / programmatic ingest)
# ============================================================

_HEALTH_SYNC_FIELDS = {"steps", "hrv", "hrv_rmssd", "resting_heart_rate", "basal_temp_delta", "sun_exposure_min", "spo2", "respiratory_rate"}

# ------------------------------------------------------------
# RMSSD artifact guard
# ------------------------------------------------------------
# The thresholds are derived from each user's own RMSSD history -- see the
# design notes in scoring.py and the caveat in MODEL.md section 7. Recomputed
# at most once per day per user: the bounds move on the timescale of months,
# and the guard runs on every sync.
_RMSSD_BOUNDS_CACHE = {}


def _rmssd_bounds_for_user(user_id: int) -> dict:
    """Return this user's calibrated RMSSD bounds, recomputing at most daily."""
    today = date.today().isoformat()
    cached = _RMSSD_BOUNDS_CACHE.get(user_id)
    if cached and cached[0] == today:
        return cached[1]
    try:
        bounds = calibrate_rmssd_bounds(db.get_rmssd_history(user_id))
    except Exception as e:
        # A calibration failure must never block a sync -- fall back to the
        # loose universal bounds, which still catch the flatly impossible.
        app.logger.warning("RMSSD calibration failed for user=%s: %s", user_id, e)
        bounds = dict(UNCALIBRATED_RMSSD_BOUNDS)
    _RMSSD_BOUNDS_CACHE[user_id] = (today, bounds)
    return bounds


@app.route("/api/health-sync", methods=["POST"])
@csrf.exempt
def api_health_sync():
    """Accept health data from iOS Shortcut or other programmatic sources.

    Auth: Bearer token from config.json["api_token"].
    Body: JSON with user_id (required), date (optional, defaults to today),
          and any subset of: steps, hrv, resting_heart_rate, basal_temp_delta,
          sun_exposure_min.
    """
    # --- auth ---
    token = CONFIG.get("api_token")
    if not token:
        return jsonify({"error": "api_token not configured"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != token:
        return jsonify({"error": "unauthorized"}), 401

    # --- parse body ---
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    user_id = body.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    # Validate user exists
    try:
        user_id = int(user_id)
        with db.get_db() as conn:
            user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": f"user_id {user_id} not found"}), 404
    except (ValueError, TypeError):
        return jsonify({"error": "user_id must be an integer"}), 400

    obs_date = body.get("date", date.today().isoformat())
    # Validate date format
    try:
        date.fromisoformat(obs_date)
    except (ValueError, TypeError):
        return jsonify({"error": f"invalid date format: {obs_date!r}, expected YYYY-MM-DD"}), 400

    # Filter to allowed fields only
    data = {"date": obs_date}
    fields_updated = []
    for field in _HEALTH_SYNC_FIELDS:
        if field in body and body[field] is not None:
            try:
                data[field] = round(float(body[field]), 2)
                fields_updated.append(field)
            except (ValueError, TypeError):
                pass

    # --- RMSSD artifact guard -------------------------------------------
    # Drop an implausible RMSSD before it reaches the model. Other fields in
    # the payload still store; a bad RMSSD does not invalidate the step count.
    rejected = []
    bounds = _rmssd_bounds_for_user(user_id)
    if "hrv_rmssd" in data:
        bad, reason = rmssd_is_implausible(data["hrv_rmssd"], data.get("hrv"), bounds)
        if bad:
            app.logger.warning("health_sync rejected RMSSD %.2f (%s) user=%s date=%s",
                               data["hrv_rmssd"], reason, user_id, obs_date)
            rejected.append({"field": "hrv_rmssd", "value": data["hrv_rmssd"],
                             "reason": reason})
            del data["hrv_rmssd"]
            fields_updated.remove("hrv_rmssd")
    elif "hrv" in data:
        # RMSSD and SDNN can arrive in SEPARATE syncs for the same day, so a
        # later payload carrying SDNN alone can retroactively expose an
        # already-stored RMSSD as an artifact -- the ingest-time check passed
        # against an SDNN that no longer exists. Re-judge what is on disk.
        stored = db.get_daily_observations(user_id, obs_date) or {}
        stored_rmssd = stored.get("hrv_rmssd")
        if stored_rmssd is not None:
            bad, reason = rmssd_is_implausible(float(stored_rmssd), data["hrv"], bounds)
            if bad:
                app.logger.warning(
                    "health_sync nulled stored RMSSD %.2f on late SDNN update (%s) "
                    "user=%s date=%s", float(stored_rmssd), reason, user_id, obs_date)
                rejected.append({"field": "hrv_rmssd", "value": float(stored_rmssd),
                                 "reason": f"late SDNN update: {reason}"})
                data["hrv_rmssd"] = None
                fields_updated.append("hrv_rmssd")

    if not fields_updated:
        # If we only rejected an artifact, that is a successful filter rather
        # than a client error -- do not signal a retry.
        if rejected:
            return jsonify({"ok": True, "date": obs_date, "fields_updated": [],
                            "rejected": rejected})
        return jsonify({"error": "no valid health fields provided"}), 400

    db.upsert_daily_observations(user_id, data)

    # Append to sync audit log so /daily can show recent syncs.
    # Wrapped in try/except so a logging failure can never break the actual sync.
    try:
        metric_payload = {f: data[f] for f in fields_updated if f in data}
        db.record_health_sync_event(
            user_id=user_id,
            posted_at=datetime.now().isoformat(timespec="seconds"),
            metric_date=obs_date,
            fields_updated=fields_updated,
            payload=metric_payload,
        )
    except Exception as e:
        app.logger.warning("health_sync_events insert failed: %s", e)

    result = {"ok": True, "date": obs_date, "fields_updated": fields_updated}
    if rejected:
        result["rejected"] = rejected
    return jsonify(result)


# ============================================================
# UV wearable ingest (device → server)
# ============================================================

# VEML6075 UV index conversion. A/B/C/D are the visible/IR compensation
# coefficients (left at 0.0 — uncompensated); UVA/UVB responsivity are the
# datasheet defaults.
_VEML_A, _VEML_B, _VEML_C, _VEML_D = 0.0, 0.0, 0.0, 0.0
_VEML_UVA_RESP = 0.001461
_VEML_UVB_RESP = 0.002591


def _veml6075_uv_index(uva: int, uvb: int, comp1: int, comp2: int) -> float:
    uva_calc = uva - _VEML_A * comp1 - _VEML_B * comp2
    uvb_calc = uvb - _VEML_C * comp1 - _VEML_D * comp2
    uvi = ((uva_calc * _VEML_UVA_RESP) + (uvb_calc * _VEML_UVB_RESP)) / 2
    return max(uvi, 0.0)


def _veml6075_sample_is_bad(uva: int, uvb: int, comp1: int, comp2: int) -> bool:
    # Failed I2C read: bus floats high, both bytes come back 0xFF, so uva
    # reads as 0xFFFF while the other channels stay near zero. A real
    # saturation event would peg multiple channels, not just one.
    if uva == 0xFFFF and uvb < 100 and comp1 < 100:
        return True
    if uvb == 0xFFFF and uva < 100 and comp1 < 100:
        return True
    return False


@app.route("/api/uv/ingest", methods=["POST"])
@csrf.exempt
def api_uv_ingest():
    """Accept a CSV tail from the uv-wearable device.

    Auth: Bearer token from config.json["wearable_token"].
    Body (text/csv): one row per line, two formats:
        sample:  boot_id,ms_since_boot,uva,uvb,comp1,comp2,batt_mv
        event:   boot_id,ms_since_boot,EVENT,<label>
    Headers used for time anchoring:
        X-Boot-Id   - the device's current boot id
        X-Device-Ms - millis() at the device when sync started
    Rows from the current boot get an absolute ts derived from request arrival
    minus device-clock skew. Rows from older boots store ts=NULL.
    """
    token = CONFIG.get("wearable_token")
    if not token:
        return jsonify({"error": "wearable_token not configured"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != token:
        return jsonify({"error": "unauthorized"}), 401

    user_id = CONFIG.get("wearable_user_id")
    if not user_id:
        return jsonify({"error": "wearable_user_id not configured"}), 500

    try:
        current_boot = int(request.headers.get("X-Boot-Id", "-1"))
        device_ms = int(request.headers.get("X-Device-Ms", "0"))
    except ValueError:
        return jsonify({"error": "bad X-Boot-Id or X-Device-Ms"}), 400

    arrival = datetime.now()
    body = request.get_data(as_text=True) or ""

    rows = []
    skipped = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 4:
            skipped += 1
            continue
        try:
            boot_id = int(parts[0])
            ms_since_boot = int(parts[1])
        except ValueError:
            skipped += 1
            continue

        if boot_id == current_boot and device_ms > 0:
            offset_ms = device_ms - ms_since_boot
            ts = (arrival - timedelta(milliseconds=offset_ms)).isoformat(timespec="seconds")
            ts_confidence = "sync_anchored"
        else:
            ts = None
            ts_confidence = None

        row = {
            "user_id": user_id, "boot_id": boot_id, "ms_since_boot": ms_since_boot,
            "ts": ts, "ts_confidence": ts_confidence,
            "uva": None, "uvb": None, "comp1": None, "comp2": None,
            "uv_index": None, "batt_mv": None, "event_label": None,
        }

        if parts[2] == "EVENT":
            row["event_label"] = ",".join(parts[3:])  # rejoin in case label has commas
        else:
            if len(parts) < 7:
                skipped += 1
                continue
            try:
                uva, uvb, comp1, comp2, batt_mv = (int(parts[i]) for i in range(2, 7))
            except ValueError:
                skipped += 1
                continue
            if _veml6075_sample_is_bad(uva, uvb, comp1, comp2):
                skipped += 1
                continue
            row["uva"], row["uvb"], row["comp1"], row["comp2"] = uva, uvb, comp1, comp2
            row["batt_mv"] = batt_mv
            row["uv_index"] = _veml6075_uv_index(uva, uvb, comp1, comp2)

        rows.append(row)

    # Chain-anchor stale-boot rows: walk newest-to-oldest, placing each boot's
    # end at the next-newer boot's start. This respects each boot's observed
    # duration (max ms_since_boot) so a 17h boot occupies 17 wall-clock hours
    # instead of being squished into a 1-minute rank slot — fixing the prior
    # bug where multiple long boots would visually overlap and produce things
    # like UV peaks at 5am.
    #
    # Anchor for the newest stale boot's end:
    #   • If we have a current-boot sync (device_ms > 0), that boot started at
    #     arrival - device_ms; assume the newest stale boot ended at that
    #     moment (zero-gap between consecutive boots).
    #   • Otherwise fall back to arrival - 1 min so something shows up.
    #
    # Caveats (intrinsic — can't fix without firmware help):
    #   • Zero-gap assumption: if the device sleeps/charges off between boots,
    #     the gap gets silently swallowed and older boots' samples shift later
    #     than reality. Per-day aggregates tolerate this better than the chart.
    #   • Lookback cap drops rows whose chained ts would be more than 24h
    #     before arrival.
    STALE_LOOKBACK_MIN = 24 * 60
    NEWEST_BOOT_END_FALLBACK_MIN = 1
    stale = [r for r in rows if r["ts"] is None]
    if stale:
        max_ms_per_boot: Dict[int, int] = {}
        for r in stale:
            b = r["boot_id"]
            max_ms_per_boot[b] = max(max_ms_per_boot.get(b, 0), r["ms_since_boot"])

        if current_boot >= 0 and device_ms > 0:
            newest_boot_end = arrival - timedelta(milliseconds=device_ms)
        else:
            newest_boot_end = arrival - timedelta(minutes=NEWEST_BOOT_END_FALLBACK_MIN)

        boot_start_dt: Dict[int, datetime] = {}
        next_boot_start = newest_boot_end
        for b in sorted(max_ms_per_boot.keys(), reverse=True):
            boot_start_dt[b] = next_boot_start - timedelta(milliseconds=max_ms_per_boot[b])
            next_boot_start = boot_start_dt[b]

        lookback_cutoff = arrival - timedelta(minutes=STALE_LOOKBACK_MIN)
        for r in stale:
            ts_dt = boot_start_dt[r["boot_id"]] + timedelta(milliseconds=r["ms_since_boot"])
            if ts_dt < lookback_cutoff:
                continue  # too far back — leave NULL
            r["ts"] = ts_dt.isoformat(timespec="seconds")
            r["ts_confidence"] = "stale_boot_approx"

    try:
        accepted = db.insert_uv_sensor_rows(rows)
    except Exception as e:
        app.logger.warning("uv_sensor insert failed: %s", e)
        return jsonify({"error": "db insert failed"}), 500

    return jsonify({
        "accepted": accepted,
        "skipped": skipped,
        "anchored_to": arrival.isoformat(timespec="seconds"),
        "stale_back_anchored": len(stale),
    })


@app.route("/api/health-sync/recent")
@login_required
def api_health_sync_recent():
    """Return the most recent health sync events for the logged-in user.
    Used by the /daily page's "Recent HealthKit Syncs" panel for live polling.
    """
    try:
        events = db.get_recent_health_sync_events(uid(), limit=3)
        return jsonify({"ok": True, "events": events})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/flare-status")
@csrf.exempt
def api_flare_status():
    """JSON flare status for iOS companion app."""
    # --- auth (same pattern as health-sync) ---
    token = CONFIG.get("api_token")
    if not token:
        return jsonify({"error": "api_token not configured"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != token:
        return jsonify({"error": "unauthorized"}), 401

    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        return jsonify({"error": "user_id must be an integer"}), 400

    # --- load observations ---
    all_obs = db.get_all_daily_observations(user_id)
    if not all_obs or len(all_obs) < 7:
        return jsonify({"ok": False, "reason": "insufficient_data"})

    all_obs.sort(key=lambda x: x['date'], reverse=True)

    # Inject multi-day scoring context (UV, burden delta, RMSSD/resp baselines)
    prefs = db.get_user_preferences(user_id) or {}
    lat = prefs.get('location_lat') or CONFIG.get('location_lat')
    lon = prefs.get('location_lon') or CONFIG.get('location_lon')
    loc_key = db.make_location_key(float(lat), float(lon)) if lat and lon else 'default'

    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, loc_key, n=7)

    last_7 = all_obs[:7]
    today_obs = last_7[0]

    # Calculate scores with user's weights
    weights = get_current_weights(user_id)
    threshold = weights.get('flare_threshold', 8.0)

    scores_7day = []
    for obs in last_7:
        score = calculate_flare_prime_score(obs, weights_override=weights)
        scores_7day.append({'date': obs['date'], 'score': score})

    today_score = scores_7day[0]['score']
    if len(scores_7day) >= 3:
        weighted_score = (
            scores_7day[0]['score'] * 1.0 +
            scores_7day[1]['score'] * 0.75 +
            scores_7day[2]['score'] * 0.5
        ) / 2.25
    else:
        weighted_score = today_score

    weighted_score = round(weighted_score, 1)
    risk_info = get_risk_level(weighted_score, threshold)
    predicted_flare = weighted_score >= threshold

    # Score delta vs yesterday
    score_delta = round(weighted_score - scores_7day[1]['score'], 1) if len(scores_7day) >= 2 else 0.0

    # Contributing factors (scoring context already injected so current_user fallbacks won't trigger)
    try:
        factors = get_contributing_factors(today_obs)
    except Exception:
        factors = []

    # Map risk level to simplified label
    level_map = {'Low Risk': 'low', 'Moderate Risk': 'moderate', 'High Risk': 'high', 'Critical Risk': 'critical'}
    risk_level = level_map.get(risk_info['level'], 'unknown')

    # Today's untaken doses
    today_str = date.today().isoformat()
    raw_doses = db.get_todays_doses(user_id, today_str)
    doses_due = []
    for d in raw_doses:
        if not d.get('taken'):
            sched_dt = d.get('scheduled_datetime', '')
            # Extract HH:MM from "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD HH:MM"
            time_part = sched_dt.split(' ', 1)[1][:5] if ' ' in sched_dt else '00:00'
            doses_due.append({
                'id': d['id'],
                'drug_name': d.get('drug_name', ''),
                'dose_label': d.get('dose_label', ''),
                'scheduled_time': time_part,
                'taken': False,
            })

    return jsonify({
        "ok": True,
        "date": today_str,
        "score": today_score,
        "weighted_score": weighted_score,
        "max_score": 25,
        "threshold": round(threshold, 1),
        "predicted_flare": predicted_flare,
        "risk_level": risk_level,
        "risk_color": risk_info['color'],
        "score_delta": score_delta,
        "delta_direction": "up" if score_delta > 0 else ("down" if score_delta < 0 else "flat"),
        "factors": [{"name": f["name"], "points": f["points"], "color": f["color"]} for f in factors],
        "doses_due": doses_due,
    })


# ============================================================
# API endpoints for Chart.js (JSON only)
# ============================================================

@app.route("/api/model")
def api_timeline():
    """JSON endpoint for model dashboard chart data."""
    end_date = request.args.get("end", date.today().isoformat())
    start_date = request.args.get(
        "start",
        (date.today() - timedelta(days=90)).isoformat()
    )
    data = db.get_timeline_data(uid(), get_location_key(), start_date, end_date)
    return jsonify(data)


@app.route("/api/uv-lag")
def api_uv_lag():
    """JSON endpoint for UV lag correlation data."""
    observations = db.get_all_daily_observations(uid())
    if not observations:
        return jsonify({"error": "no data"})
    start = observations[0]["date"]
    end = observations[-1]["date"]
    uv_data = db.get_uv_data_range(get_location_key(), start, end)
    return jsonify({"observations": observations, "uv": uv_data})
