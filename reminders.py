"""
Background jobs and ntfy push notifications: medication reminders,
flare-risk alerts, UV fetch checks, daily and period nudges.

Importing this module starts the scheduler.
"""

import os
from datetime import date, datetime, timedelta
import db
import uv_fetcher
from apscheduler.schedulers.background import BackgroundScheduler

from appcore import CONFIG
from flaremodel import _compute_phase_by_date_from_obs, _inject_cycle_phase, _inject_scoring_context, calculate_flare_prime_score, get_contributing_factors, get_current_weights, get_risk_level


# ============================================================
# Medication reminder notifications (ntfy)
# ============================================================

# Embedded platforms (the Android local app) set SARDINE_NOTIFY_QUEUE: instead
# of pushing via ntfy, notifications are queued in the database and the host OS
# drains + delivers them natively (see db.queue_notification / drain).
_NOTIFY_QUEUE = bool(os.environ.get("SARDINE_NOTIFY_QUEUE"))


def _send_ntfy(message: str) -> None:
    """Send a push notification via ntfy.sh (or self-hosted ntfy server)."""
    if _NOTIFY_QUEUE:
        db.queue_notification("Medication Reminder", message, "high", "pill")
        return
    import requests as _requests
    topic = CONFIG.get("ntfy_topic")
    server = CONFIG.get("ntfy_server", "https://ntfy.sh")
    if not topic:
        return
    try:
        _requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": "Medication Reminder",
                "Priority": "high",
                "Tags": "pill",
            },
            timeout=5,
        )
    except Exception as e:
        print(f"[reminder] ntfy send failed: {e}")


def _send_ntfy_to(server: str, topic: str, message: str) -> None:
    """Send a push notification to a specific ntfy server/topic."""
    if _NOTIFY_QUEUE:
        db.queue_notification("Medication Reminder", message, "high", "pill")
        return
    import requests as _requests
    try:
        _requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": "Medication Reminder",
                "Priority": "high",
                "Tags": "pill",
            },
            timeout=5,
        )
    except Exception as e:
        print(f"[reminder] ntfy send failed: {e}")


def _send_ntfy_alert(message: str, title: str, priority: str = "default",
                     tags: str = "warning", server: str = None,
                     topic: str = None) -> None:
    """Send a push notification with custom title, priority, and tags.
    If server/topic not provided, falls back to global CONFIG.
    """
    if _NOTIFY_QUEUE:
        db.queue_notification(title, message, priority, tags)
        return
    import requests as _requests
    topic = topic or CONFIG.get("ntfy_topic")
    server = server or CONFIG.get("ntfy_server", "https://ntfy.sh")
    if not topic:
        return
    try:
        _requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
            },
            timeout=5,
        )
    except Exception as e:
        print(f"[ntfy-alert] send failed: {e}")


def _check_flare_risk_alert() -> None:
    """Daily cron job: send ntfy flare warning when risk is elevated or cycle phase is changing.
    Loops over all users with ntfy configured.
    """
    today_str = date.today().isoformat()
    users = db.get_users_with_ntfy()
    if not users:
        return

    MODERATE_THRESHOLD = 5.0
    HIGH_THRESHOLD = 8.0

    for user in users:
        user_id = user["user_id"]
        topic = user["ntfy_topic"]
        server = user.get("ntfy_server") or "https://ntfy.sh"

        # Rate limit: only one alert per user per calendar day
        if user.get("last_flare_alert_date") == today_str:
            continue

        # Load observations and inject cycle phase
        all_obs = db.get_all_daily_observations(user_id)
        if not all_obs or len(all_obs) < 3:
            continue
        all_obs.sort(key=lambda x: x["date"], reverse=True)
        _inject_cycle_phase(all_obs)

        # Inject multi-day scoring context
        loc_key = db.make_location_key(
            user.get("location_lat") or CONFIG.get("location_lat", 0),
            user.get("location_lon") or CONFIG.get("location_lon", 0),
        )
        obs_by_date = {o["date"]: o for o in all_obs}
        _inject_scoring_context(all_obs, obs_by_date, loc_key, n=3)

        # Load user's flare threshold
        user_weights = get_current_weights(user_id)
        user_threshold = user_weights.get('flare_threshold', 8.0)

        # 3-day weighted average score
        scores = [calculate_flare_prime_score(obs) for obs in all_obs[:3]]
        w3 = [1.0, 0.75, 0.5]
        weighted_score = sum(s * w for s, w in zip(scores, w3)) / sum(w3)

        # Tomorrow's cycle phase (forward-looking)
        tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
        phase_by_date = _compute_phase_by_date_from_obs(all_obs)
        tomorrow_phase = phase_by_date.get(tomorrow_str)
        entering_high_risk_tomorrow = tomorrow_phase in ("pms", "luteal")
        today_phase = all_obs[0].get("cycle_phase_name") if all_obs else None

        should_alert = weighted_score >= MODERATE_THRESHOLD or entering_high_risk_tomorrow
        if not should_alert:
            continue

        # Build message body
        risk_info = get_risk_level(weighted_score, user_threshold)
        risk_label = risk_info["level"]

        factors = get_contributing_factors(all_obs[0])
        top_factors = ", ".join(f["name"] for f in factors[:3]) if factors else ""

        lines = [f"Score: {weighted_score:.1f}  |  {risk_label}"]
        if top_factors:
            lines.append(f"Factors: {top_factors}")
        if entering_high_risk_tomorrow and today_phase not in ("pms", "luteal"):
            lines.append(f"Entering {tomorrow_phase} phase tomorrow.")
        elif today_phase in ("pms", "luteal"):
            lines.append(f"Currently in {today_phase} phase.")

        message = "\n".join(lines)
        priority = "high" if weighted_score >= HIGH_THRESHOLD else "default"
        tags = "rotating_light" if weighted_score >= HIGH_THRESHOLD else "warning"

        _send_ntfy_alert(message, title=f"Flare risk: {risk_label}",
                         priority=priority, tags=tags,
                         server=server, topic=topic)

        # Persist per-user rate limit in user_preferences
        try:
            db.upsert_user_preferences(user_id, {"last_flare_alert_date": today_str})
        except Exception as e:
            print(f"[flare-alert] state save failed for user {user_id}: {e}")


def _check_uv_fetch() -> None:
    """Daily cron job: fetch UV data for each distinct user location.
    Alerts users via ntfy if their location's UV fetch fails.
    """
    today_str = date.today().isoformat()

    # Fetch UV for each distinct location
    locations = db.get_distinct_user_locations()
    failed_location_keys = set()

    for loc in locations:
        lat, lon = loc["location_lat"], loc["location_lon"]
        location_key = db.make_location_key(lat, lon)
        uv = uv_fetcher.fetch_and_store_uv_for_date(today_str, location_key=location_key)
        if uv is None:
            failed_location_keys.add(location_key)

    # Alert users whose locations failed (only those with ntfy configured)
    if not failed_location_keys:
        return

    users = db.get_users_with_ntfy()
    for user in users:
        # Rate limit per user
        if user.get("last_uv_alert_date") == today_str:
            continue

        lat = user.get("location_lat")
        lon = user.get("location_lon")
        if not lat or not lon:
            continue

        user_loc_key = db.make_location_key(lat, lon)
        if user_loc_key not in failed_location_keys:
            continue

        _send_ntfy_alert(
            f"Could not fetch UV index data for {today_str}. "
            "Open-Meteo may be unreachable. Enter UV manually on today's entry.",
            title="UV data unavailable",
            priority="default",
            tags="satellite",
            server=user.get("ntfy_server") or "https://ntfy.sh",
            topic=user["ntfy_topic"],
        )
        try:
            db.upsert_user_preferences(user["user_id"], {"last_uv_alert_date": today_str})
        except Exception as e:
            print(f"[uv-alert] state save failed for user {user['user_id']}: {e}")


def _check_and_send_reminders(lookback_minutes: int = 0) -> None:
    """Background job: send ntfy notifications for doses due in the next minute.

    [lookback_minutes] widens the window into the past — the classic scheduler
    runs every minute so 0 is right; embedded hosts poll less often and pass a
    lookback so doses due between wakeups still notify (a late reminder beats
    a missing one). The notified flag keeps repeats out either way.
    """
    now = datetime.now()
    window_start = now - timedelta(minutes=lookback_minutes)
    window_end = now + timedelta(minutes=1)
    try:
        pending = db.get_all_pending_doses_with_ntfy(
            window_start.strftime("%Y-%m-%d %H:%M"),
            window_end.strftime("%Y-%m-%d %H:%M"),
        )
        for dose in pending:
            # Send to user's own ntfy topic
            topic = dose.get("ntfy_topic")
            server = dose.get("ntfy_server") or "https://ntfy.sh"
            if topic or _NOTIFY_QUEUE:
                _send_ntfy_to(server, topic, dose["dose_label"])
            db.mark_dose_notified(dose["id"])
    except Exception as e:
        print(f"[reminder] scheduler error: {e}")


def _check_daily_reminders() -> None:
    """Hourly job: send a 'log your day' ntfy reminder to users who haven't
    logged within their configured reminder_hours window (e.g. 16 hours).
    Rate-limited to one reminder per calendar day per user."""
    try:
        users = db.get_users_with_ntfy()
        if not users:
            return

        for user in users:
            reminder_hours = user.get("reminder_hours")
            if not reminder_hours:
                continue  # Not enabled

            # Determine user's current time
            tz_name = user.get("timezone") or CONFIG.get("timezone", "UTC")
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tz_name)
                user_now = datetime.now(tz)
            except Exception:
                user_now = datetime.now()

            today_str = user_now.strftime("%Y-%m-%d")

            # Rate limit: one reminder per calendar day
            if user.get("last_reminder_date") == today_str:
                continue

            # Check time since last log
            last_logged = user.get("last_logged_at")
            if last_logged:
                try:
                    logged_dt = datetime.fromisoformat(last_logged)
                    # Make naive for comparison if needed
                    if logged_dt.tzinfo is None and user_now.tzinfo is not None:
                        logged_dt = logged_dt.replace(tzinfo=user_now.tzinfo)
                    hours_since = (user_now - logged_dt).total_seconds() / 3600
                    if hours_since < reminder_hours:
                        continue  # Logged recently enough
                except Exception:
                    pass  # Bad timestamp, proceed with reminder
            # If last_logged_at is None, they've never logged — remind them

            topic = user["ntfy_topic"]
            server = user.get("ntfy_server") or "https://ntfy.sh"
            display = user.get("display_name") or user.get("username", "")
            _send_ntfy_alert(
                f"Hey {display} — it's been a while since your last log. Even a quick entry helps.",
                title="Daily log reminder",
                priority="low",
                tags="memo",
                server=server,
                topic=topic,
            )

            # Mark reminder sent for today
            try:
                db.upsert_user_preferences(user["user_id"], {"last_reminder_date": today_str})
            except Exception as e:
                print(f"[daily-reminder] state save failed for user {user['user_id']}: {e}")
    except Exception as e:
        print(f"[daily-reminder] error: {e}")


def _check_period_nudge() -> None:
    """Hourly job: nudge users who logged period flow 4 days ago but nothing since.
    Helps keep cycle tracking accurate by prompting continued flow logging."""
    try:
        users = db.get_users_with_ntfy()
        if not users:
            return

        for user in users:
            if not user.get("track_cycle"):
                continue

            user_id = user["user_id"]
            topic = user["ntfy_topic"]
            server = user.get("ntfy_server") or "https://ntfy.sh"

            tz_name = user.get("timezone") or CONFIG.get("timezone", "UTC")
            try:
                from zoneinfo import ZoneInfo
                user_now = datetime.now(ZoneInfo(tz_name))
            except Exception:
                user_now = datetime.now()

            today_str = user_now.strftime("%Y-%m-%d")

            if user.get("last_period_nudge_date") == today_str:
                continue

            # Find recent flow entries
            all_obs = db.get_all_daily_observations(user_id)
            if not all_obs:
                continue

            # Look for most recent day with any flow
            last_flow_date = None
            for obs in reversed(all_obs):
                if obs.get("period_flow") and obs["period_flow"] != "":
                    last_flow_date = obs["date"]
                    break

            if not last_flow_date:
                continue

            days_since = (date.fromisoformat(today_str) - date.fromisoformat(last_flow_date)).days
            if days_since != 4:
                continue

            # Check no flow logged between last_flow_date and today
            gap_has_flow = False
            for obs in all_obs:
                if last_flow_date < obs["date"] <= today_str:
                    if obs.get("period_flow") and obs["period_flow"] != "":
                        gap_has_flow = True
                        break
            if gap_has_flow:
                continue

            display = user.get("display_name") or user.get("username", "")
            _send_ntfy_alert(
                f"Hey {display} — still on your period? Log today's flow to keep cycle tracking accurate.",
                title="Period tracking reminder",
                priority="low",
                tags="drop_of_blood",
                server=server,
                topic=topic,
            )

            try:
                db.upsert_user_preferences(user_id, {"last_period_nudge_date": today_str})
            except Exception as e:
                print(f"[period-nudge] state save failed for user {user_id}: {e}")
    except Exception as e:
        print(f"[period-nudge] error: {e}")


# Start scheduler — guard against Flask reloader double-start
# CONFIG["debug"] is available at import time (unlike app.debug which is set later by app.run).
_is_reloader_parent = (
    CONFIG.get("debug", False) and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
)

# Embedded mode (the Android local app): the host platform owns scheduling
# (WorkManager/AlarmManager) because the OS kills background processes —
# an in-process scheduler would silently never fire.
_embedded = bool(os.environ.get("SARDINE_EMBEDDED"))

if not _is_reloader_parent and not _embedded:
    _tz = CONFIG.get("timezone", "UTC")
    _scheduler = BackgroundScheduler(timezone=_tz)
    _scheduler.add_job(_check_and_send_reminders, "interval", minutes=1,
                       id="reminders", replace_existing=True)
    _alert_hour = CONFIG.get("flare_alert_hour", 8)
    _scheduler.add_job(_check_flare_risk_alert, "cron", hour=_alert_hour, minute=0,
                       id="flare_alert", replace_existing=True)
    _uv_alert_hour = CONFIG.get("uv_alert_hour", 13)
    _scheduler.add_job(_check_uv_fetch, "cron", hour=_uv_alert_hour, minute=0,
                       id="uv_fetch", replace_existing=True)
    _scheduler.add_job(_check_daily_reminders, "cron", minute=0,
                       id="daily_reminders", replace_existing=True)
    _scheduler.add_job(_check_period_nudge, "cron", minute=30,
                       id="period_nudge", replace_existing=True)
    _scheduler.start()
