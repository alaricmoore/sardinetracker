"""
The model dashboard page (/model) and its export, UV lag analysis, the
UV wearable view, and HRV. Named dashboard so flaremodel.py is the only
"model" file.
"""

from scoring import UV_PROTECTION_MULTIPLIERS, weighted_uv
import json
from datetime import datetime, timedelta
from flask import render_template, request, Response
from flask_login import login_required
import db
from typing import Optional, Dict, List

from appcore import app, get_location_key, uid
from flaremodel import _inject_cycle_phase, _inject_scoring_context, _score_components, get_current_weights


@app.route("/model")
def timeline():
    """Model dashboard — score attribution over time.
    Endpoint name preserved as 'timeline' so existing url_for('timeline') calls keep working."""
    days_param = request.args.get("days", "60")
    try:
        n_days = int(days_param)
    except ValueError:
        n_days = 0  # 0 = all

    all_obs = db.get_all_daily_observations(uid())
    if not all_obs:
        return render_template("timeline.html", has_data=False)

    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}
    subset = all_obs[:n_days] if n_days else all_obs
    _inject_scoring_context(subset, obs_by_date, get_location_key())

    weights = get_current_weights(uid())
    threshold = weights.get('flare_threshold', 8.0)

    # Compute per-day score components
    daily_data = []
    flare_scores = []
    nonflare_scores = []

    for obs in reversed(subset):  # chronological order
        comp = _score_components(obs)
        flare = obs.get('flare_occurred') == 1
        severity = obs.get('flare_severity')

        entry = {
            'date': obs['date'],
            'total': comp['total'],
            'uv': comp['uv'],
            'exertion': comp['exertion'],
            'temperature': comp['temperature'],
            'symptoms': comp['symptoms'],
            'pain_fatigue': comp['pain_fatigue'],
            'cycle': comp['cycle'],
            'burden_delta': comp['burden_delta'],
            'rmssd': comp['rmssd'],
            'rmssd_instability': comp['rmssd_instability'],
            'resp_rate': comp['resp_rate'],
            'flare': flare,
            'severity': severity,
            # Raw values for multi-day predictor panel
            'burden_delta_raw': obs.get('_symptom_burden_delta'),
            'rmssd_deviation_raw': obs.get('_rmssd_deviation'),
            'rmssd_instability_raw': obs.get('_rmssd_instability'),
            'resp_rate_deviation_raw': obs.get('_resp_rate_deviation'),
        }
        daily_data.append(entry)

        if flare:
            flare_scores.append(comp['total'])
        else:
            nonflare_scores.append(comp['total'])

    # Score distribution stats
    def _dist_stats(vals):
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        return {
            'min': round(s[0], 1),
            'q1': round(s[n // 4], 1),
            'median': round(s[n // 2], 1),
            'q3': round(s[3 * n // 4], 1),
            'max': round(s[-1], 1),
            'mean': round(sum(s) / n, 1),
            'n': n,
        }

    distribution = {
        'flare': _dist_stats(flare_scores),
        'nonflare': _dist_stats(nonflare_scores),
    }

    return render_template(
        "timeline.html",
        has_data=True,
        daily_json=json.dumps(daily_data),
        threshold=threshold,
        distribution=distribution,
        n_days=len(subset),
        days_param=days_param,
    )

@app.route("/model/export")
@login_required
def timeline_export():
    """Export daily score + component breakdown as CSV."""
    from io import StringIO
    import csv

    all_obs = db.get_all_daily_observations(uid())
    if not all_obs:
        return "No data", 404

    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)
    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key())

    weights = get_current_weights(uid())
    threshold = weights.get('flare_threshold', 8.0)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'date', 'total_score', 'predicted_flare', 'actual_flare', 'flare_severity',
        'uv', 'exertion', 'temperature', 'symptoms', 'pain_fatigue',
        'burden_delta', 'rmssd', 'resp_rate',
        'burden_delta_raw', 'rmssd_deviation_raw', 'resp_rate_deviation_raw',
        # Appended rather than inserted, so anything reading columns by
        # position keeps working. With these, the parts sum to total_score.
        'rmssd_instability', 'cycle',
    ])

    for obs in reversed(all_obs):
        comp = _score_components(obs)
        writer.writerow([
            obs['date'],
            comp['total'],
            'Y' if comp['total'] >= threshold else '',
            'Y' if obs.get('flare_occurred') == 1 else '',
            obs.get('flare_severity') or '',
            comp['uv'], comp['exertion'], comp['temperature'],
            comp['symptoms'], comp['pain_fatigue'],
            comp['burden_delta'], comp['rmssd'], comp['resp_rate'],
            obs.get('_symptom_burden_delta') or '',
            obs.get('_rmssd_deviation') or '',
            obs.get('_resp_rate_deviation') or '',
            comp['rmssd_instability'], comp['cycle'],
        ])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=flare_scores.csv'},
    )


# ============================================================
# UV lag analysis
# ============================================================

def compute_lag_correlations(observations: list, uv_data: list) -> dict:
    """Compute Pearson correlation between UV dose and each symptom
    at lag windows of 0, 1, 2, and 3 days.

    UV dose = (weighted_UV^1.5) × sun exposure minutes × protection multiplier
    UV dose on day D is correlated against symptom on day D+lag.
    Exponential weighting reflects that high UV is disproportionately more damaging.
    
    A high correlation at lag=2 means UV exposure predicts
    that symptom two days later.

    Args:
        observations: list of daily_observation dicts (must include sun_exposure_min)
        uv_data: list of uv_data dicts (includes uv_morning, uv_noon, uv_evening)

    Returns:
        dict of {symptom_name: {lag_0: {...}, lag_1: {...}, ...}}
        Each lag contains: r, p, n, significant
    """
    import numpy as np
    from scipy import stats

    # Build date-indexed lookups
    obs_by_date = {o["date"]: o for o in observations}
    uv_by_date  = {u["date"]: u for u in uv_data}

    # Sorted date list that has UV, observation, AND sun exposure data
    dates_with_all = sorted([
        d for d in obs_by_date
        if d in uv_by_date
        and weighted_uv(uv_by_date[d]) > 0
        and obs_by_date[d].get("sun_exposure_min") is not None
    ])

    if len(dates_with_all) < 10:
        return {}

    # Symptom targets - continuous scales and boolean flags
    targets = {
        "pain":          lambda o: o.get("pain_scale"),
        "fatigue":       lambda o: o.get("fatigue_scale"),
        "neurological":  lambda o: o.get("neurological"),
        "musculature":   lambda o: o.get("musculature"),
        "migraine":      lambda o: o.get("migraine"),
        "cognitive":     lambda o: o.get("cognitive"),
        "dermatological":lambda o: o.get("dermatological"),
        "pulmonary":     lambda o: o.get("pulmonary"),
        "rheumatic":     lambda o: o.get("rheumatic"),
        "gastro":        lambda o: o.get("gastro"),
        "mucosal":       lambda o: o.get("mucosal"),
        "flare":         lambda o: o.get("flare_occurred"),
    }

    lag_days = [0, 1, 2, 3, 4]
    results = {}

    for symptom_name, getter in targets.items():
        results[symptom_name] = {}

        for lag in lag_days:
            uv_doses = []
            sym_vals = []

            for i, date_str in enumerate(dates_with_all):
                # UV dose = weighted UV × minutes × protection multiplier
                obs = obs_by_date[date_str]
                sun_min = obs.get("sun_exposure_min")

                if sun_min is None:
                    continue

                w_uv = weighted_uv(uv_by_date[date_str])
                protection = UV_PROTECTION_MULTIPLIERS.get(
                    obs.get("uv_protection_level", "none"), 1.0)
                uv_dose = (w_uv ** 1.5) * float(sun_min) * protection

                # Find the date lag days later
                lag_date = (
                    datetime.strptime(date_str, "%Y-%m-%d") +
                    timedelta(days=lag)
                ).strftime("%Y-%m-%d")

                lag_obs = obs_by_date.get(lag_date)
                if lag_obs is None:
                    continue

                sym_val = getter(lag_obs)
                if sym_val is None:
                    continue

                uv_doses.append(uv_dose)
                sym_vals.append(float(sym_val))

            # Need at least 8 paired observations for meaningful correlation
            if len(uv_doses) < 8:
                results[symptom_name][f"lag_{lag}"] = None
                continue

            uv_arr  = np.array(uv_doses)
            sym_arr = np.array(sym_vals)

            # Skip if no variance (all zeros e.g. rare symptom or always indoors)
            if uv_arr.std() == 0 or sym_arr.std() == 0:
                results[symptom_name][f"lag_{lag}"] = None
                continue

            r, p_value = stats.pearsonr(uv_arr, sym_arr)
            
            # Very strict significance for multiple comparisons (9 symptoms × 4 lags = 36 tests)
            # p < 0.0005 and |r| >= 0.35 (medium-to-large effect size)
            results[symptom_name][f"lag_{lag}"] = {
                "r":       round(float(r), 3),
                "p":       round(float(p_value), 4),
                "n":       len(uv_doses),
                "significant": float(p_value) < 0.0005 and abs(float(r)) >= 0.35,
            }

    return results


def _compute_personal_lag_summary(user_id: int) -> Optional[dict]:
    """Compute average |r| across all symptoms for each lag day.
    Returns {lag_0: avg_r, lag_1: ..., lag_2: ..., lag_3: ..., lag_4: ..., best_lag: int} or None.
    Window matches the 4-day cumulative UV lookback used by the scoring model.
    """
    observations = db.get_all_daily_observations(user_id)
    if not observations or len(observations) < 10:
        return None

    start_date = observations[0]["date"]
    end_date = observations[-1]["date"]
    location_key = get_location_key()
    uv_data = db.get_uv_data_range(location_key, start_date, end_date)

    correlations = compute_lag_correlations(observations, uv_data)
    if not correlations:
        return None

    lag_avgs = {}
    for lag_idx in range(5):
        lag_key = f"lag_{lag_idx}"
        r_values = []
        for symptom, lags in correlations.items():
            entry = lags.get(lag_key)
            if entry and entry.get('r') is not None:
                r_values.append(abs(entry['r']))
        lag_avgs[lag_key] = round(sum(r_values) / len(r_values), 3) if r_values else 0

    best_lag = max(range(5), key=lambda i: lag_avgs[f"lag_{i}"])
    lag_avgs['best_lag'] = best_lag
    return lag_avgs


@app.route("/uv-lag")
def uv_lag():
    """UV lag correlation analysis view."""
    observations = db.get_all_daily_observations(uid())
    if not observations:
        return render_template("uv_lag.html", has_data=False)

    start_date = observations[0]["date"]
    end_date   = observations[-1]["date"]
    uv_data    = db.get_uv_data_range(get_location_key(), start_date, end_date)

    if not uv_data:
        return render_template("uv_lag.html", has_data=False,
                               no_uv_message=True)

    correlations = compute_lag_correlations(observations, uv_data)

    return render_template(
        "uv_lag.html",
        has_data=True,
        correlations_json=json.dumps(correlations, default=lambda x: int(x) if isinstance(x, bool) else str(x)),
        n_observations=len(observations),
        n_uv_days=len(uv_data),
        start_date=start_date,
        end_date=end_date,
    )


# ============================================================
# UV wearable view
# ============================================================


def _uv_bucket_minutes(hours: int) -> int:
    """Bucket width for the chart based on view range.

    Raw resolution for short windows; coarser bins as range grows so we
    don't try to render thousands of points at the same x-tick. Returns
    0 to mean 'no bucketing, pass raw samples through'. hours=0 means
    all-time, which uses daily buckets.
    """
    if hours == 0:
        return 1440  # all-time → daily buckets
    if hours <= 6:
        return 0
    if hours <= 72:
        return 15
    if hours <= 168:
        return 30
    if hours <= 720:    # ≤ 1 month
        return 60
    if hours <= 4320:   # ≤ 6 months
        return 360      # 6-hour buckets
    return 1440         # daily


def _bucket_uv_samples(samples: list[dict], bucket_minutes: int) -> list[dict]:
    """Average samples into fixed-width time bins anchored to the hour.

    A bin's ts_confidence is 'sync_anchored' only if every contributing row
    was sync_anchored; otherwise 'stale_boot_approx' so the chart keeps the
    dimmed-point visual for any bin that's even partly approximate.
    """
    if bucket_minutes <= 0 or not samples:
        return samples

    bins: Dict[str, List[dict]] = {}
    for s in samples:
        ts = s.get("ts")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        floored = (dt.hour * 60 + dt.minute) // bucket_minutes * bucket_minutes
        key = dt.strftime("%Y-%m-%dT") + f"{floored // 60:02d}:{floored % 60:02d}:00"
        bins.setdefault(key, []).append(s)

    def _mean(rows: List[dict], field: str) -> Optional[float]:
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    out = []
    for key in sorted(bins.keys()):
        rows = bins[key]
        confs = {r.get("ts_confidence") for r in rows}
        bin_conf = "sync_anchored" if confs == {"sync_anchored"} else "stale_boot_approx"
        out.append({
            "ts": key,
            "ts_confidence": bin_conf,
            "uva": _mean(rows, "uva"),
            "uvb": _mean(rows, "uvb"),
            "comp1": _mean(rows, "comp1"),
            "comp2": _mean(rows, "comp2"),
            "uv_index": _mean(rows, "uv_index"),
            "batt_mv": _mean(rows, "batt_mv"),
            "event_label": None,
            "n": len(rows),  # how many raw samples folded into this bin
        })
    return out


def _uv_stats(samples: List[dict]) -> dict:
    """Mean and peak summary for the stats panel. Computed on the raw
    samples, not the bucketed view, so the averages reflect sample density."""
    if not samples:
        return {}

    def _mean(field: str) -> Optional[float]:
        vals = [s[field] for s in samples if s.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    def _peak(field: str) -> Optional[float]:
        vals = [s[field] for s in samples if s.get(field) is not None]
        return max(vals) if vals else None

    n_anchored = sum(1 for s in samples if s.get("ts_confidence") == "sync_anchored")
    return {
        "n": len(samples),
        "n_anchored": n_anchored,
        "n_approx": len(samples) - n_anchored,
        "mean_uva": _mean("uva"),
        "mean_uvb": _mean("uvb"),
        "mean_uv_index": _mean("uv_index"),
        "mean_comp1": _mean("comp1"),
        "mean_comp2": _mean("comp2"),
        "peak_uv_index": _peak("uv_index"),
        "peak_uva": _peak("uva"),
        "peak_uvb": _peak("uvb"),
    }


# UVI 3.0 is "moderate" on the WHO scale (sunburn risk for unprotected skin).
# Used as the threshold for "outdoor / direct sun exposure" rather than ambient
# indoor light. Useful for lupus risk because the photo-trigger from UV-B
# exposure correlates with crossing into the moderate/high range, not with
# integrated low-UV ambient.
_UV_HIGH_THRESHOLD = 3.0
_UV_SAMPLE_INTERVAL_MIN = 5  # device samples every ~5 minutes


def _uv_daily_summary(samples: List[dict]) -> List[dict]:
    """Per-day UV summary: peak UVI, hours above the moderate threshold.

    Less sensitive to within-day chain-anchor mis-placement than the chart is,
    because a sample landing at 5am vs noon doesn't change the day's peak or
    its total hours-above-threshold. Designed for the lupus use case where
    daily UV dose matters more than precise timing.

    'approx_only' flags days that have only stale_boot_approx data (no
    sync-anchored samples) — those days' totals are most affected by gap
    swallowing across boots.
    """
    if not samples:
        return []

    by_day: Dict[str, dict] = {}
    for s in samples:
        ts = s.get("ts")
        uvi = s.get("uv_index")
        if not ts or uvi is None:
            continue
        day = ts[:10]
        d = by_day.setdefault(day, {
            "day": day,
            "peak_uv": 0.0,
            "n_samples": 0,
            "n_above_threshold": 0,
            "approx_only": True,
        })
        if uvi > d["peak_uv"]:
            d["peak_uv"] = uvi
        d["n_samples"] += 1
        if uvi >= _UV_HIGH_THRESHOLD:
            d["n_above_threshold"] += 1
        if s.get("ts_confidence") == "sync_anchored":
            d["approx_only"] = False

    for d in by_day.values():
        d["hours_above_threshold"] = round(
            d["n_above_threshold"] * _UV_SAMPLE_INTERVAL_MIN / 60, 1
        )

    return sorted(by_day.values(), key=lambda d: d["day"])


_WEARABLE_RANGES = [
    (24,   "24h",   "last 24 hours"),
    (168,  "1w",    "last week"),
    (720,  "1mo",   "last month"),
    (4320, "6mo",   "last 6 months"),
    (0,    "all",   "all time"),
]


@app.route("/wearable")
def wearable():
    """UV wearable sensor readings.

    Stale-boot back-anchored rows are shown by default with dim/small point
    styling — they're approximate (rank-based heuristic can't perfectly
    reconstruct chronology across many C3 reboots) but they're most of the
    data. Pass ?include_approx=0 to hide them.
    """
    hours = request.args.get("hours", default=24, type=int)
    # hours == 0 is the all-time sentinel; otherwise clamp to a sane upper bound.
    if hours != 0:
        hours = max(1, min(hours, 24 * 365 * 5))
    include_approx = request.args.get("include_approx", default=1, type=int) != 0

    rows = db.get_recent_uv_sensor_readings(uid(), hours=hours)
    n_approx_hidden = sum(1 for r in rows if r["ts_confidence"] == "stale_boot_approx")
    if not include_approx:
        rows = [r for r in rows if r["ts_confidence"] != "stale_boot_approx"]
    samples_raw = [r for r in rows if r["event_label"] is None]
    events      = [r for r in rows if r["event_label"] is not None]

    # Stats are computed BEFORE bucketing so the averages reflect real
    # sample density, not bin density. Bucketing only shapes the chart.
    stats          = _uv_stats(samples_raw)
    bucket_minutes = _uv_bucket_minutes(hours)
    samples        = _bucket_uv_samples(samples_raw, bucket_minutes)
    daily_summary  = _uv_daily_summary(samples_raw)

    range_label = next((lbl for h, _, lbl in _WEARABLE_RANGES if h == hours), f"last {hours}h")

    return render_template(
        "wearable.html",
        has_data=bool(samples_raw),
        hours=hours,
        range_label=range_label,
        range_options=_WEARABLE_RANGES,
        include_approx=include_approx,
        n_approx_hidden=n_approx_hidden if not include_approx else 0,
        bucket_minutes=bucket_minutes,
        samples_json=json.dumps(samples),
        events_json=json.dumps(events),
        daily_summary_json=json.dumps(daily_summary),
        daily_summary=daily_summary,
        n_samples=len(samples_raw),
        n_buckets=len(samples) if bucket_minutes else None,
        n_events=len(events),
        stats=stats,
        uv_high_threshold=_UV_HIGH_THRESHOLD,
    )
