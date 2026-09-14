"""
Interventions and their evaluation, birth control history, the cycle view,
medication events, taper schedules and doses.
"""

from scoring import weighted_uv
import calendar
import json
from datetime import date, datetime, timedelta
from flask import jsonify, render_template, request, redirect, url_for
from flask_login import login_required
import db
from typing import Optional

from appcore import CONFIG, app, get_user_prefs, uid
from flaremodel import _detect_ovulation_bbt, _detect_period_starts


# ============================================================
# HRV and autonomic
# ============================================================


# ============================================================
# Intervention evaluation helpers (used by /interventions view)
# ============================================================

_EVENT_TYPES = ('side_effect', 'rebound', 'efficacy_change', 'dose_change', 'note')


def _days_between(d1: str, d2: str) -> int:
    """Inclusive-exclusive day count from d1 to d2."""
    a = datetime.strptime(d1, "%Y-%m-%d").date()
    b = datetime.strptime(d2, "%Y-%m-%d").date()
    return (b - a).days


def _date_plus(d: str, days: int) -> str:
    return (datetime.strptime(d, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()


def _filter_obs(observations: list, start: str, end: str) -> list:
    """Observations with start <= date < end (half-open)."""
    return [o for o in observations if start <= o['date'] < end]


def _filter_obs_inclusive(observations: list, start: str, end: str) -> list:
    """Observations with start <= date <= end (closed)."""
    return [o for o in observations if start <= o['date'] <= end]


def compute_flare_stats(pre_obs: list, post_obs: list) -> dict:
    """Pre/post flare count, severity breakdown, mean gap, and delta percentages."""

    def _severity_counts(obs: list) -> dict:
        flare_dates = []
        major = minor = er = 0
        for o in obs:
            if o.get('flare_occurred') != 1:
                continue
            flare_dates.append(o['date'])
            sev = o.get('flare_severity')
            if sev == 'er_visit':
                er += 1
            elif sev == 'major':
                major += 1
            elif sev == 'minor':
                minor += 1
        return {
            'count': len(flare_dates),
            'major': major,
            'minor': minor,
            'er': er,
            'dates': sorted(flare_dates),
        }

    def _mean_gap_days(dates: list) -> Optional[float]:
        if len(dates) < 2:
            return None
        gaps = [_days_between(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]
        return round(sum(gaps) / len(gaps), 1)

    pre = _severity_counts(pre_obs)
    post = _severity_counts(post_obs)
    pre['mean_gap_days'] = _mean_gap_days(pre['dates'])
    post['mean_gap_days'] = _mean_gap_days(post['dates'])

    def _delta_pct(a: int, b: int) -> Optional[float]:
        if a == 0:
            return None
        return round((b - a) / a * 100, 1)

    return {
        'pre': pre,
        'post': post,
        'delta_rate_pct': _delta_pct(pre['count'], post['count']),
        'delta_major_pct': _delta_pct(pre['major'], post['major']),
        'delta_minor_pct': _delta_pct(pre['minor'], post['minor']),
        'delta_er_pct': _delta_pct(pre['er'], post['er']),
    }


def compute_autonomic_stats(pre_obs: list, post_obs: list) -> dict:
    """Mean ± SD and n for RMSSD, SDNN, and respiratory rate over pre/post windows."""
    import numpy as np

    def _stats(obs: list, field: str) -> dict:
        vals = [o.get(field) for o in obs if o.get(field) is not None]
        vals = [float(v) for v in vals]
        if not vals:
            return {'mean': None, 'std': None, 'n': 0}
        arr = np.array(vals)
        return {
            'mean': round(float(arr.mean()), 2),
            'std': round(float(arr.std()), 2) if len(vals) > 1 else 0.0,
            'n': len(vals),
        }

    return {
        'rmssd': {'pre': _stats(pre_obs, 'hrv_rmssd'), 'post': _stats(post_obs, 'hrv_rmssd')},
        'sdnn': {'pre': _stats(pre_obs, 'hrv'), 'post': _stats(post_obs, 'hrv')},
        'resp': {'pre': _stats(pre_obs, 'respiratory_rate'), 'post': _stats(post_obs, 'respiratory_rate')},
    }


def _days_to_return_to_baseline(pre_obs: list, post_obs: list, field: str,
                                consecutive_needed: int = 7) -> Optional[int]:
    """Days post-start until the metric stays within pre-mean ± 1 SD for
    `consecutive_needed` consecutive daily observations. None if never."""
    pre_vals = [float(o[field]) for o in pre_obs if o.get(field) is not None]
    if len(pre_vals) < 4:
        return None
    pre_mean = sum(pre_vals) / len(pre_vals)
    if len(pre_vals) < 2:
        pre_std = 0.0
    else:
        m = pre_mean
        pre_std = (sum((x - m) ** 2 for x in pre_vals) / len(pre_vals)) ** 0.5
    lo, hi = pre_mean - pre_std, pre_mean + pre_std
    post_vals = sorted(
        [(o['date'], float(o[field])) for o in post_obs if o.get(field) is not None],
        key=lambda t: t[0]
    )
    if not post_vals:
        return None
    start = post_vals[0][0]
    run = 0
    run_start = None
    for d, v in post_vals:
        if lo <= v <= hi:
            if run == 0:
                run_start = d
            run += 1
            if run >= consecutive_needed:
                return _days_between(start, run_start)
        else:
            run = 0
            run_start = None
    return None


def compute_duration_of_effect(med: dict, observations: list, window_days: int = 60) -> dict:
    """For one-time interventions: days-to-next-flare per severity and
    days-until-autonomic-baseline-return (within ±1 SD of pre-mean for 7 days)."""
    start = med['start_date']
    post_end = _date_plus(start, window_days)
    pre_start = _date_plus(start, -window_days)

    pre_obs = _filter_obs(observations, pre_start, start)
    post_obs = _filter_obs_inclusive(observations, start, post_end)

    def _days_to_next_flare(severities: tuple) -> Optional[int]:
        for o in sorted(post_obs, key=lambda x: x['date']):
            if o.get('flare_occurred') == 1 and o.get('flare_severity') in severities:
                return _days_between(start, o['date'])
        return None

    return {
        'days_to_next_minor': _days_to_next_flare(('minor',)),
        'days_to_next_major': _days_to_next_flare(('major',)),
        'days_to_next_er': _days_to_next_flare(('er_visit',)),
        'days_to_any_flare': _days_to_next_flare(('minor', 'major', 'er_visit')),
        'days_to_rmssd_baseline': _days_to_return_to_baseline(pre_obs, post_obs, 'hrv_rmssd'),
        'days_to_sdnn_baseline': _days_to_return_to_baseline(pre_obs, post_obs, 'hrv'),
        'days_to_resp_baseline': _days_to_return_to_baseline(pre_obs, post_obs, 'respiratory_rate'),
    }


def compute_rebound_flag(med: dict, observations: list) -> dict:
    """Auto-detect possible rebound: flare rate in days 14-45 post >> baseline,
    with low rate in days 0-13. Returns {'show': bool, 'message': str}."""
    start = med['start_date']
    pre_30 = _filter_obs(observations, _date_plus(start, -30), start)
    initial = _filter_obs(observations, start, _date_plus(start, 14))
    rebound = _filter_obs_inclusive(observations, _date_plus(start, 14), _date_plus(start, 45))

    today = date.today().isoformat()
    if _date_plus(start, 45) > today:
        return {'show': False}

    baseline_rate = sum(1 for o in pre_30 if o.get('flare_occurred') == 1) / 30
    initial_rate = sum(1 for o in initial if o.get('flare_occurred') == 1) / 14
    rebound_n = sum(1 for o in rebound if o.get('flare_occurred') == 1)
    rebound_rate = rebound_n / 32

    if rebound_rate > 1.5 * baseline_rate and initial_rate < 0.5 * baseline_rate and rebound_n >= 2:
        pre_n = sum(1 for o in pre_30 if o.get('flare_occurred') == 1)
        initial_n = sum(1 for o in initial if o.get('flare_occurred') == 1)
        return {
            'show': True,
            'message': (f"Possible rebound: {rebound_n} flares in days 14-45 post-dose vs "
                        f"{initial_n} in days 0-13 and {pre_n} in the 30 days before.")
        }
    return {'show': False}


def compute_intervention_card(med: dict, observations: list, events: list,
                              fixed_window: int) -> dict:
    """Bundle all pre/post analysis for one intervention into a single dict
    consumed by the interventions.html template."""
    start = med['start_date']
    end = med.get('end_date')
    today = date.today().isoformat()

    if end is None or end >= today:
        is_ongoing = True
        end_effective = today
    else:
        is_ongoing = False
        end_effective = end

    duration_days = _days_between(start, end_effective)
    is_one_time = (not is_ongoing) and duration_days <= 3

    if is_ongoing:
        days_active = _days_between(start, today)
        pre_start = _date_plus(start, -days_active)
        pre_end = start
        post_start = start
        post_end = today
        window_label = f"matched · {days_active} days"
    else:
        w = fixed_window if fixed_window > 0 else 9999
        pre_start = _date_plus(start, -w)
        pre_end = start
        post_start = start
        post_end = _date_plus(start, w)
        window_label = f"{fixed_window}-day fixed" if fixed_window > 0 else "all available"

    pre_obs = _filter_obs(observations, pre_start, pre_end)
    post_obs = _filter_obs_inclusive(observations, post_start, post_end)

    card = {
        'med': med,
        'is_ongoing': is_ongoing,
        'is_one_time': is_one_time,
        'duration_days': duration_days,
        'window_label': window_label,
        'pre_window': [pre_start, pre_end],
        'post_window': [post_start, post_end],
        'flare_stats': compute_flare_stats(pre_obs, post_obs),
        'autonomic_stats': compute_autonomic_stats(pre_obs, post_obs),
        'duration_of_effect': compute_duration_of_effect(med, observations, fixed_window or 60) if is_one_time else None,
        'rebound_flag': compute_rebound_flag(med, observations) if is_one_time else {'show': False},
        'events': events,
        'event_counts_by_type': _count_events_by_type(events),
    }
    return card


def _count_events_by_type(events: list) -> dict:
    counts = {t: 0 for t in _EVENT_TYPES}
    for e in events:
        t = e.get('event_type')
        if t in counts:
            counts[t] += 1
    return counts


def compute_hrv_data(observations: list, intervention_date: str = None) -> dict:
    """Compute HRV trend with 7-day rolling average and intervention split.
    Includes SDNN (hrv), RMSSD (hrv_rmssd), and respiratory rate when available.

    Includes any observation that has at least one autonomic metric (hrv,
    rmssd, or respiratory rate), so resp-only days still appear on the
    respiratory rate charts even when HRV is missing.
    """
    import numpy as np

    auto_obs = [o for o in observations
                if o.get("hrv") is not None
                or o.get("hrv_rmssd") is not None
                or o.get("respiratory_rate") is not None]
    if not auto_obs:
        return {}

    dates    = [o["date"] for o in auto_obs]
    hrv_vals = [float(o["hrv"]) if o.get("hrv") is not None else None for o in auto_obs]
    rmssd_vals = [float(o["hrv_rmssd"]) if o.get("hrv_rmssd") is not None else None for o in auto_obs]
    resp_vals = [float(o["respiratory_rate"]) if o.get("respiratory_rate") is not None else None for o in auto_obs]

    def _rolling_avg(vals, min_n=3):
        result = []
        for i in range(len(vals)):
            window = [v for v in vals[max(0, i - 6): i + 1] if v is not None]
            result.append(round(sum(window) / len(window), 2) if len(window) >= min_n else None)
        return result

    rolling = _rolling_avg(hrv_vals)
    rmssd_rolling = _rolling_avg(rmssd_vals)
    # Resp rate can be sparse — accept a single data point so isolated observations still render
    resp_rolling = _rolling_avg(resp_vals, min_n=1)

    # Split stats only if intervention date is provided
    pre_vals  = []
    post_vals = []
    pre_rmssd = []
    post_rmssd = []
    pre_resp = []
    post_resp = []
    if intervention_date:
        pre_vals  = [v for d, v in zip(dates, hrv_vals) if d < intervention_date and v is not None]
        post_vals = [v for d, v in zip(dates, hrv_vals) if d >= intervention_date and v is not None]
        pre_rmssd = [v for d, v in zip(dates, rmssd_vals) if d < intervention_date and v is not None]
        post_rmssd = [v for d, v in zip(dates, rmssd_vals) if d >= intervention_date and v is not None]
        pre_resp = [v for d, v in zip(dates, resp_vals) if d < intervention_date and v is not None]
        post_resp = [v for d, v in zip(dates, resp_vals) if d >= intervention_date and v is not None]

    def stats_dict(vals):
        if not vals:
            return {"mean": None, "std": None, "n": 0}
        arr = np.array(vals)
        return {"mean": round(float(arr.mean()), 2),
                "std":  round(float(arr.std()), 2),
                "n":    len(vals)}

    pre_stats = stats_dict(pre_vals)
    post_stats = stats_dict(post_vals)
    pre_stats["rmssd_mean"] = stats_dict(pre_rmssd)["mean"]
    pre_stats["rmssd_std"] = stats_dict(pre_rmssd)["std"]
    post_stats["rmssd_mean"] = stats_dict(post_rmssd)["mean"]
    post_stats["rmssd_std"] = stats_dict(post_rmssd)["std"]
    pre_stats["resp_mean"] = stats_dict(pre_resp)["mean"]
    pre_stats["resp_std"] = stats_dict(pre_resp)["std"]
    post_stats["resp_mean"] = stats_dict(post_resp)["mean"]
    post_stats["resp_std"] = stats_dict(post_resp)["std"]

    return {
        "dates":          dates,
        "hrv_raw":        hrv_vals,
        "hrv_rolling":    rolling,
        "rmssd_raw":      rmssd_vals,
        "rmssd_rolling":  rmssd_rolling,
        "resp_raw":       resp_vals,
        "resp_rolling":   resp_rolling,
        "pre_intervention":  pre_stats,
        "post_intervention": post_stats,
    }


def compute_sleep_bbt_uv(observations: list, location_key: str = 'default') -> dict:
    """Build sleep/BBT dataset paired with UV from the previous day (lag 1).

    For each observation that has sleep or BBT data, look up weighted UV
    (morning/noon/evening) from the day before. Returns aligned arrays for charting.
    """
    import db as _db

    obs_by_date = {o["date"]: o for o in observations}
    all_dates = sorted(obs_by_date.keys())

    dates      = []
    sleep_vals = []
    bbt_vals   = []
    uv_lag1    = []

    for date_str in all_dates:
        obs = obs_by_date[date_str]
        sleep = obs.get("hours_slept")
        bbt   = obs.get("basal_temp_delta")

        if sleep is None and bbt is None:
            continue

        # Get weighted UV from the previous day
        prev_date = (datetime.strptime(date_str, "%Y-%m-%d") -
                     timedelta(days=1)).strftime("%Y-%m-%d")
        uv_row = _db.get_uv_data(location_key, prev_date)
        w_uv = weighted_uv(uv_row) if uv_row else None

        dates.append(date_str)
        sleep_vals.append(float(sleep) if sleep is not None else None)
        bbt_vals.append(float(bbt) if bbt is not None else None)
        uv_lag1.append(w_uv)

    return {
        "dates":      dates,
        "sleep":      sleep_vals,
        "bbt":        bbt_vals,
        "uv_lag1":    uv_lag1,
    }


# ============================================================
# BC (contraceptive) classification — derived, not stored
# ============================================================
BC_IS_HORMONAL = {
    "combined_pill", "progestin_only_pill", "hormonal_iud",
    "implant", "patch", "ring", "injection",
}
BC_CONTAINS_ESTROGEN = {"combined_pill", "patch", "ring"}

BC_TYPE_LABELS = {
    "none":               "no BC",
    "combined_pill":      "combined pill (estrogen + progestin)",
    "progestin_only_pill":"progestin-only pill",
    "hormonal_iud":       "hormonal IUD",
    "copper_iud":         "copper IUD",
    "implant":            "implant",
    "patch":              "patch (estrogen + progestin)",
    "ring":               "ring (estrogen + progestin)",
    "injection":          "injection (progestin)",
    "barrier":            "barrier method",
    "other":              "other",
}


@app.route("/bc/add", methods=["POST"])
def bc_add():
    db.add_bc_regime(uid(), {
        "bc_type":    request.form.get("bc_type", "none"),
        "name":       request.form.get("name") or None,
        "start_date": request.form.get("start_date"),
        "end_date":   request.form.get("end_date") or None,
        "notes":      request.form.get("notes") or None,
    })
    return redirect(url_for("cycle_view"))


@app.route("/bc/delete/<int:bc_id>", methods=["POST"])
def bc_delete(bc_id):
    db.delete_bc_regime(uid(), bc_id)
    return redirect(url_for("cycle_view"))


@app.route("/bc/update/<int:bc_id>", methods=["POST"])
def bc_update(bc_id):
    db.update_bc_regime(uid(), bc_id, {
        "bc_type":    request.form.get("bc_type", "none"),
        "name":       request.form.get("name") or None,
        "start_date": request.form.get("start_date"),
        "end_date":   request.form.get("end_date") or None,
        "notes":      request.form.get("notes") or None,
    })
    return redirect(url_for("cycle_view"))


@app.route("/cycle")
def cycle_view():
    """Menstrual cycle calendar — opt-in via user preferences."""
    prefs = get_user_prefs()
    if not prefs.get("track_cycle", CONFIG.get("track_cycle")):
        return redirect(url_for("daily_entry"))

    year  = request.args.get("year",  type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)

    # Fetch 12 months of history for cycle-length calculation, plus the current month
    history_start = (date(year, month, 1) - timedelta(days=365)).isoformat()
    month_last_day = calendar.monthrange(year, month)[1]
    month_end = date(year, month, month_last_day).isoformat()
    all_data = db.get_cycle_data(uid(), history_start, month_end)

    # Build BBT lookup for the entire history window
    bbt_by_date = {
        row["date"]: row["basal_temp_delta"]
        for row in all_data
        if row.get("basal_temp_delta") is not None
    }

    # Detect period start days (3-day min, spotting retroactive, 3-day gap to close)
    period_starts = _detect_period_starts(all_data)

    # Average cycle length — use last 6 cycles, discard gaps > 90 days (data holes, not cycles)
    lengths_raw: list[int] = []
    avg_cycle = 28
    if len(period_starts) >= 2:
        lengths_raw = [
            (date.fromisoformat(period_starts[i + 1]) -
             date.fromisoformat(period_starts[i])).days
            for i in range(len(period_starts) - 1)
        ]
        lengths = [l for l in lengths_raw if l <= 90]
        recent = lengths[-6:] if lengths else []
        avg_cycle = round(sum(recent) / len(recent)) if recent else 28

    # Build phase lookup for ALL historical cycles using BBT-detected ovulation where available
    phase_by_date: dict[str, str] = {}
    bbt_ovulations: dict[str, date] = {}  # period_start_str -> detected ovulation date

    for i, start_str in enumerate(period_starts):
        cycle_start = date.fromisoformat(start_str)
        cycle_end = (date.fromisoformat(period_starts[i + 1])
                     if i + 1 < len(period_starts)
                     else cycle_start + timedelta(days=avg_cycle))

        detected_ov = _detect_ovulation_bbt(bbt_by_date, cycle_start, cycle_end)
        if detected_ov:
            bbt_ovulations[start_str] = detected_ov
            lut = detected_ov
        else:
            lut = cycle_end - timedelta(days=14)

        pms = lut + timedelta(days=7)
        d = lut
        while d < cycle_end:
            phase_by_date[d.isoformat()] = "pms" if d >= pms else "luteal"
            d += timedelta(days=1)

    # Forward prediction for current (open) cycle — prefer BBT-detected ovulation
    next_period = pms_start = ovulation = luteal_start = None
    ovulation_source = "predicted"
    if period_starts:
        last_start = date.fromisoformat(period_starts[-1])
        detected_ov = _detect_ovulation_bbt(
            bbt_by_date, last_start, date.today() + timedelta(days=1)
        )
        if detected_ov:
            ovulation = detected_ov
            luteal_start = detected_ov
            next_period = detected_ov + timedelta(days=14)
            ovulation_source = "detected"
        else:
            next_period = last_start + timedelta(days=avg_cycle)
            ovulation = next_period - timedelta(days=14)
            luteal_start = ovulation

        pms_start = next_period - timedelta(days=7)

        # Extend phase_by_date forward into the predicted future
        d = luteal_start
        while d < next_period:
            if d.isoformat() not in phase_by_date:
                phase_by_date[d.isoformat()] = "pms" if d >= pms_start else "luteal"
            d += timedelta(days=1)

    # Filter observation data to current month for the display grid
    month_start_str = date(year, month, 1).isoformat()
    month_data = {
        row["date"]: row for row in all_data
        if row["date"] >= month_start_str
    }

    # BBT data points for the current month in calendar order (None if no data)
    bbt_points = []
    for d_num in range(1, month_last_day + 1):
        ds = date(year, month, d_num).isoformat()
        obs = month_data.get(ds)
        bbt = obs["basal_temp_delta"] if obs and obs.get("basal_temp_delta") is not None else None
        bbt_points.append((d_num, bbt))

    # Intervention markers: (drug_name, 'start', category) for new starts this month,
    # (drug_name, 'active', category) on day-1 for meds active from a prior month
    all_meds = db.get_all_medications(uid())
    intervention_dates: dict = {}
    for m in all_meds:
        if not (m.get("is_primary_intervention") or m.get("is_secondary_intervention")):
            continue
        s = m["start_date"]
        e = m.get("end_date")
        cat = m.get("category", "prescription")
        if month_start_str <= s <= month_end:
            intervention_dates[s] = (m["drug_name"], "start", cat)
        elif s < month_start_str and (e is None or e >= month_start_str):
            if month_start_str not in intervention_dates:
                intervention_dates[month_start_str] = (m["drug_name"], "active", cat)

    # Flare counts by cycle phase (across all history)
    phase_day_counts: dict[str, int] = {"pms": 0, "luteal": 0, "follicular": 0, "period": 0}
    flare_phase_counts: dict[str, int] = {"pms": 0, "luteal": 0, "follicular": 0, "period": 0}
    for row in all_data:
        ds = row["date"]
        if bool(row.get("period_flow") and row["period_flow"] != "spotting"):
            ph = "period"
        else:
            ph = phase_by_date.get(ds, "follicular")
        phase_day_counts[ph] = phase_day_counts.get(ph, 0) + 1
        if row.get("flare_occurred"):
            flare_phase_counts[ph] = flare_phase_counts.get(ph, 0) + 1

    # Phase analytics: symptom frequency + biometrics by phase
    # Uses full observations (SELECT *) to access symptom booleans, HRV, pain, fatigue
    _SYMPTOM_KEYS = [
        "neurological", "cognitive", "musculature", "migraine",
        "pulmonary", "dermatological", "rheumatic", "gastro", "mucosal",
    ]
    _SYMPTOM_LABELS = {
        "neurological": "Neurological", "cognitive": "Cognitive",
        "musculature": "Musculature", "migraine": "Migraine",
        "pulmonary": "Pulmonary", "dermatological": "Dermatological",
        "rheumatic": "Rheumatic", "gastro": "Gastrointestinal",
        "mucosal": "Mucosal",
    }
    _DISPLAY_PHASES = ("period", "follicular", "luteal")

    all_obs_full = db.get_daily_observations_range(uid(), history_start, month_end)
    bc_history   = db.get_bc_history(uid())  # sorted start_date ASC

    def _bc_for_date(date_str: str) -> dict | None:
        """Return the active BC record for a given date, or None."""
        for bc in reversed(bc_history):
            if bc["start_date"] <= date_str:
                if bc["end_date"] is None or bc["end_date"] >= date_str:
                    return bc
        return None

    def _empty_buckets() -> dict:
        return {
            p: {"sym": {k: 0 for k in _SYMPTOM_KEYS}, "hrv": [],
                "pain": [], "fat": [], "n": 0}
            for p in _DISPLAY_PHASES
        }

    buckets_all      = _empty_buckets()
    buckets_hormonal = _empty_buckets()
    buckets_no_bc    = _empty_buckets()

    for obs in all_obs_full:
        ds     = obs["date"]
        raw_ph = phase_by_date.get(ds)
        if obs.get("period_flow") and obs["period_flow"] not in ("", None, "spotting"):
            dp = "period"
        elif raw_ph in ("pms", "luteal"):
            dp = "luteal"
        else:
            dp = "follicular"

        bc       = _bc_for_date(ds)
        bc_type  = bc["bc_type"] if bc else None
        hormonal = bc_type in BC_IS_HORMONAL

        for bkt in (buckets_all,
                    buckets_hormonal if hormonal else buckets_no_bc):
            bkt[dp]["n"] += 1
            for k in _SYMPTOM_KEYS:
                if obs.get(k):
                    bkt[dp]["sym"][k] += 1
            if obs.get("hrv") is not None:
                bkt[dp]["hrv"].append(obs["hrv"])
            if obs.get("pain_scale") is not None:
                bkt[dp]["pain"].append(obs["pain_scale"])
            if obs.get("fatigue_scale") is not None:
                bkt[dp]["fat"].append(obs["fatigue_scale"])

    def _pm(lst: list) -> float | None:
        return round(sum(lst) / len(lst), 1) if lst else None

    def _bkt_to_analytics(bkt: dict) -> dict:
        return {
            p: {
                "days":    bkt[p]["n"],
                "hrv":     _pm(bkt[p]["hrv"]),
                "pain":    _pm(bkt[p]["pain"]),
                "fatigue": _pm(bkt[p]["fat"]),
                "symptoms": {
                    k: round(bkt[p]["sym"][k] / bkt[p]["n"] * 100)
                    if bkt[p]["n"] else 0
                    for k in _SYMPTOM_KEYS
                },
            }
            for p in _DISPLAY_PHASES
        }

    phase_analytics          = _bkt_to_analytics(buckets_all)
    phase_analytics_hormonal = _bkt_to_analytics(buckets_hormonal)
    phase_analytics_no_bc    = _bkt_to_analytics(buckets_no_bc)

    # Show stratification toggle only when both strata have ≥30 days of follicular data
    # (follicular is the baseline / largest phase — a reliable proxy for overall coverage)
    show_bc_toggle = (
        phase_analytics_hormonal["follicular"]["days"] >= 30
        and phase_analytics_no_bc["follicular"]["days"] >= 30
    )

    def _sym_rows(pa: dict) -> list:
        return sorted(
            [{"key": k, "label": _SYMPTOM_LABELS[k],
              "period":     pa["period"]["symptoms"][k],
              "follicular": pa["follicular"]["symptoms"][k],
              "luteal":     pa["luteal"]["symptoms"][k]}
             for k in _SYMPTOM_KEYS],
            key=lambda r: r["luteal"], reverse=True,
        )

    symptom_rows          = _sym_rows(phase_analytics)
    symptom_rows_hormonal = _sym_rows(phase_analytics_hormonal)
    symptom_rows_no_bc    = _sym_rows(phase_analytics_no_bc)

    # Per-cycle length series with BC annotation
    cycle_length_series = []
    if len(period_starts) >= 2:
        for i in range(len(period_starts) - 1):
            length = (date.fromisoformat(period_starts[i + 1]) -
                      date.fromisoformat(period_starts[i])).days
            if 15 <= length <= 60:
                bc       = _bc_for_date(period_starts[i])
                bc_type  = bc["bc_type"] if bc else None
                cycle_length_series.append({
                    "date":        period_starts[i],
                    "length":      length,
                    "bc_type":     bc_type or "none",
                    "is_hormonal": bc_type in BC_IS_HORMONAL if bc_type else False,
                })

    # Intervention cycle-length effects (up to 3 cycles before/after each intervention)
    intervention_effects = []
    for m in all_meds:
        if not (m.get("is_primary_intervention") or m.get("is_secondary_intervention")):
            continue
        s = m["start_date"]
        before = [l for ps, l in zip(period_starts, lengths_raw) if ps < s][-3:]
        after  = [l for ps, l in zip(period_starts[1:], lengths_raw) if ps > s][:3]
        if before or after:
            intervention_effects.append({
                "drug":       m["drug_name"],
                "start":      s,
                "before_avg": round(sum(before) / len(before)) if before else None,
                "after_avg":  round(sum(after)  / len(after))  if after  else None,
            })

    # Month navigation
    prev_year,  prev_month  = (year - 1, 12) if month == 1  else (year, month - 1)
    next_year,  next_month  = (year + 1, 1)  if month == 12 else (year, month + 1)

    return render_template(
        "cycle.html",
        year=year, month=month,
        month_name=date(year, month, 1).strftime("%B %Y"),
        month_data=month_data,
        month_last_day=month_last_day,
        phase_by_date=phase_by_date,
        avg_cycle=avg_cycle,
        next_period=next_period,
        pms_start=pms_start,
        ovulation=ovulation,
        ovulation_source=ovulation_source,
        luteal_start=luteal_start,
        period_starts=period_starts,
        bbt_points=bbt_points,
        bbt_ovulations=bbt_ovulations,
        intervention_dates=intervention_dates,
        flare_phase_counts=flare_phase_counts,
        phase_day_counts=phase_day_counts,
        intervention_effects=intervention_effects,
        phase_analytics=phase_analytics,
        phase_analytics_hormonal=phase_analytics_hormonal,
        phase_analytics_no_bc=phase_analytics_no_bc,
        show_bc_toggle=show_bc_toggle,
        symptom_rows=symptom_rows,
        symptom_rows_hormonal=symptom_rows_hormonal,
        symptom_rows_no_bc=symptom_rows_no_bc,
        cycle_length_series=cycle_length_series,
        bc_history=bc_history,
        bc_type_labels=BC_TYPE_LABELS,
        bc_is_hormonal=list(BC_IS_HORMONAL),
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
        cal=calendar,
    )


@app.route("/cycle/flow", methods=["POST"])
@login_required
def cycle_flow_log():
    """Quick-log period flow from the cycle calendar."""
    data = request.get_json(force=True)
    date_str = data.get("date")
    flow_level = data.get("flow_level", "")

    if not date_str:
        return jsonify({"error": "date required"}), 400
    if flow_level not in ("", "spotting", "light", "medium", "heavy"):
        return jsonify({"error": "invalid flow_level"}), 400

    db.upsert_daily_observations(uid(), {
        "date": date_str,
        "period_flow": flow_level if flow_level else None,
    })
    return jsonify({"ok": True})


@app.route("/interventions")
def hrv_view():
    """Intervention evaluation: per-medication pre/post flare + autonomic stats,
    duration-of-effect for one-time doses, and structured side-effect logging.
    Endpoint name 'hrv_view' preserved so existing url_for calls keep working."""
    user_id = uid()
    observations = db.get_all_daily_observations(user_id)
    all_meds = db.get_all_medications(user_id)

    # Window selector — affects one-time intervention cards only
    try:
        fixed_window = int(request.args.get("window", "60"))
    except ValueError:
        fixed_window = 60
    if fixed_window not in (30, 60, 90, 120, 0):
        fixed_window = 60

    # Pick interventions (primary + secondary); primary first, then secondary by start_date desc
    interventions = [m for m in all_meds
                     if m.get("is_primary_intervention") or m.get("is_secondary_intervention")]
    interventions.sort(key=lambda m: (
        0 if m.get("is_primary_intervention") else 1,
        # Newest first within each tier
        "" if m.get("is_primary_intervention") else m.get("start_date", "0")
    ), reverse=False)
    # Secondary sorted newest first (above sort mixes ascending start dates; fix for secondaries)
    primary = [m for m in interventions if m.get("is_primary_intervention")]
    secondary = sorted(
        [m for m in interventions if not m.get("is_primary_intervention")],
        key=lambda m: m.get("start_date", ""), reverse=True
    )
    interventions = primary + secondary

    cards = []
    for m in interventions:
        events = db.get_medication_events(user_id, m["id"])
        cards.append(compute_intervention_card(m, observations, events, fixed_window))

    # Global HRV trend across all time (preserved from old /autonomic view)
    global_hrv = compute_hrv_data(observations, intervention_date=None)

    flare_events = [
        {"date": o["date"], "severity": o.get("flare_severity") or "minor"}
        for o in observations if o.get("flare_occurred") == 1
    ]
    intervention_lines = [
        {"date": m["start_date"], "name": m["drug_name"],
         "category": m.get("category", "prescription"),
         "is_primary": bool(m.get("is_primary_intervention"))}
        for m in interventions
    ]

    return render_template(
        "interventions.html",
        has_data=bool(global_hrv) or bool(cards),
        cards=cards,
        global_hrv_json=json.dumps(global_hrv, default=lambda x: int(x) if isinstance(x, bool) else str(x)),
        flare_events_json=json.dumps(flare_events),
        intervention_lines_json=json.dumps(intervention_lines),
        fixed_window=fixed_window,
        today_iso=date.today().isoformat(),
    )


# ============================================================
# Medication events CRUD
# ============================================================

def _parse_event_severity(raw, event_type: str):
    """Severity is required for side_effect (0-10), null otherwise."""
    if event_type != 'side_effect':
        return None
    if raw in (None, ''):
        return None
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return None
    return max(0, min(10, v))


@app.route("/intervention/<int:med_id>/event/add", methods=["POST"])
def add_medication_event(med_id: int):
    """Log a new event (side effect, rebound, dose change, etc.) for a medication."""
    user_id = uid()
    # Verify the medication belongs to the user
    med = db.get_medication(user_id, med_id)
    if not med:
        return redirect(url_for("hrv_view"))

    event_type = request.form.get("event_type", "note").strip()
    if event_type not in _EVENT_TYPES:
        event_type = "note"
    event_date = request.form.get("event_date") or date.today().isoformat()
    severity = _parse_event_severity(request.form.get("severity"), event_type)
    note = (request.form.get("note") or "").strip() or None

    db.add_medication_event(user_id, med_id, event_date, event_type, severity, note)
    return redirect(url_for("hrv_view") + f"#med-{med_id}")


@app.route("/intervention/event/<int:event_id>/update", methods=["POST"])
def update_medication_event(event_id: int):
    """Update a medication event, scoped to the current user."""
    user_id = uid()
    existing = db.get_medication_event(user_id, event_id)
    if not existing:
        return redirect(url_for("hrv_view"))

    event_type = request.form.get("event_type", existing["event_type"]).strip()
    if event_type not in _EVENT_TYPES:
        event_type = existing["event_type"]
    event_date = request.form.get("event_date") or existing["event_date"]
    severity = _parse_event_severity(request.form.get("severity"), event_type)
    note = (request.form.get("note") or "").strip() or None

    db.update_medication_event(user_id, event_id, event_date, event_type, severity, note)
    return redirect(url_for("hrv_view") + f"#med-{existing['medication_id']}")


@app.route("/intervention/event/<int:event_id>/delete", methods=["POST"])
def delete_medication_event(event_id: int):
    """Delete a medication event, scoped to the current user."""
    user_id = uid()
    existing = db.get_medication_event(user_id, event_id)
    if existing:
        db.delete_medication_event(user_id, event_id)
        med_id = existing["medication_id"]
        return redirect(url_for("hrv_view") + f"#med-{med_id}")
    return redirect(url_for("hrv_view"))


# ============================================================
# Taper schedules and dose reminders
# ============================================================

@app.route("/taper/create", methods=["POST"])
def taper_create():
    """Create a taper schedule with individual dose rows from the wizard form."""
    med_id = int(request.form.get("medication_id"))
    start_date = request.form.get("start_date")
    drug_name = request.form.get("drug_name", "medication")
    unit = request.form.get("unit", "tablet(s)")

    # Build dose rows from form fields: dose_label_N, dose_time_N, dose_amount_N
    doses_raw = {}
    for key, val in request.form.items():
        if key.startswith("dose_label_"):
            idx = key[len("dose_label_"):]
            doses_raw.setdefault(idx, {})["label"] = val
        elif key.startswith("dose_time_"):
            idx = key[len("dose_time_"):]
            doses_raw.setdefault(idx, {})["time"] = val
        elif key.startswith("dose_amount_"):
            idx = key[len("dose_amount_"):]
            doses_raw.setdefault(idx, {})["amount"] = val

    schedule_id = db.create_taper_schedule(uid(), med_id, start_date)

    dose_rows = []
    for idx in sorted(doses_raw.keys(), key=lambda x: int(x)):
        entry = doses_raw[idx]
        label = entry.get("label", "")
        time_str = entry.get("time", "08:00")
        amount = entry.get("amount")
        # datetime-local inputs submit as 'YYYY-MM-DDTHH:MM'; normalize to 'YYYY-MM-DD HH:MM'
        normalized_dt = time_str.replace("T", " ")[:16]
        dose_rows.append({
            "taper_schedule_id": schedule_id,
            "medication_id": med_id,
            "scheduled_datetime": normalized_dt,
            "dose_label": label,
            "dose_amount": float(amount) if amount else None,
            "dose_unit": unit,
        })

    db.insert_scheduled_doses(uid(), dose_rows)
    return redirect(url_for("clinical_record") + "#medications")


@app.route("/taper/delete/<int:schedule_id>", methods=["POST"])
def taper_delete(schedule_id):
    """Delete a taper schedule and all its doses."""
    db.delete_taper_schedule(uid(), schedule_id)
    return redirect(url_for("clinical_record") + "#medications")


@app.route("/dose/take/<int:dose_id>", methods=["POST"])
def dose_take(dose_id):
    """Mark a dose as taken."""
    taken_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.mark_dose_taken(dose_id, taken_at)
    # Redirect back to wherever the user came from (daily entry or clinical)
    return_url = request.form.get("return_url", url_for("daily_entry"))
    return redirect(return_url)


@app.route("/doses/today")
def doses_today():
    """JSON endpoint: today's scheduled doses."""
    today_str = date.today().isoformat()
    doses = db.get_todays_doses(uid(), today_str)
    return jsonify(doses)
