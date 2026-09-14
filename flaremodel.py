"""
The flare model's shared scoring layer: weights, multi-day context, data
provenance, cycle detection, and the flare score. Forecast, the dashboard,
daily, reports, the API and the reminders all use it, so it lives in none of them.
"""

import json
import os
from datetime import date, datetime, timedelta
from flask_login import current_user
import db
from severity_vocab import severity_score
from typing import Optional

from appcore import CONFIG, DATA_DIR, get_user_prefs


# ============================================================
# LAB ADJUSTMENTS
# ============================================================

# The pure scoring primitives now live in scoring.py, so analysis scripts and
# summarize.py can use them without importing this module — importing app runs
# db.run_migrations() and starts a BackgroundScheduler, which a read-only
# reporting tool has no business doing.
#
# Re-exported into this namespace so every existing call site below, and the
# `from app import ...` in analysis_cycle_vs_hrv.py, keeps working unchanged.
from scoring import (  # noqa: F401
    UNCALIBRATED_RMSSD_BOUNDS,
    UV_PROTECTION_MULTIPLIERS,
    _SYMPTOM_KEYS,
    calibrate_rmssd_bounds,
    rmssd_is_implausible,
    _compute_resp_rate_deviation,
    _compute_rmssd_deviation,
    _compute_rmssd_instability,
    _compute_symptom_burden_delta,
    _daily_symptom_count,
    compute_rmssd,
    weighted_uv,
)


def _compute_cumulative_uv(obs_date: str, obs_by_date: dict, location_key: str) -> float:
    """Compute prior-4-day cumulative UV dose.
    Decay: yesterday 0.8×, day-before 0.6×, 3 days ago 0.4×, 4 days ago 0.2×.
    Personal data analysis showed UV signal persists 2-4 days before major
    flares (unprotected ≥60 min days: 79% at day-1, 67% at day-2/-3, 58% at day-4
    vs 35-40% non-flare baseline). Earlier 3-day window with 0.7/0.4/0.2 decay
    dropped off too aggressively for a signal that stays visible.
    Same-day UV is already handled by the main scoring block.
    """
    decay = [(1, 0.8), (2, 0.6), (3, 0.4), (4, 0.2)]
    total = 0.0
    target = datetime.strptime(obs_date, "%Y-%m-%d").date()

    for offset, w in decay:
        d = (target - timedelta(days=offset)).isoformat()
        uv_row = db.get_uv_data(location_key, d)
        if not uv_row:
            continue
        w_uv = weighted_uv(uv_row)
        prior_obs = obs_by_date.get(d, {})
        sun_min = float(prior_obs.get('sun_exposure_min') or 0)
        protection = UV_PROTECTION_MULTIPLIERS.get(
            prior_obs.get('uv_protection_level') or 'none', 1.0)
        total += (w_uv ** 1.5) * sun_min * protection * w

    return total


def _inject_scoring_context(obs_list: list, obs_by_date: dict, loc_key: str,
                            n: int | None = None) -> None:
    """Inject multi-day scoring context into observations in-place.

    Enriches each obs with multi-day context so calculate_flare_prime_score()
    has access to rolling baselines and cumulative metrics.
    """
    subset = obs_list[:n] if n else obs_list
    for obs in subset:
        obs['_uv_row'] = db.get_uv_data(loc_key, obs['date'])
        obs['_cumulative_uv_dose'] = _compute_cumulative_uv(obs['date'], obs_by_date, loc_key)
        obs['_symptom_burden_delta'] = _compute_symptom_burden_delta(obs['date'], obs_by_date)
        obs['_rmssd_deviation'] = _compute_rmssd_deviation(obs['date'], obs_by_date)
        obs['_rmssd_instability'] = _compute_rmssd_instability(obs['date'], obs_by_date)
        obs['_resp_rate_deviation'] = _compute_resp_rate_deviation(obs['date'], obs_by_date)


# Default weights (factory settings)
# Symptom weights control points added per symptom checked.
# Category multipliers scale entire scoring categories (1.0 = default).
DEFAULT_WEIGHTS = {
    # Symptom weights
    'neurological': 1.5,
    'cognitive': 1.0,
    'musculature': 1.5,
    'migraine': 1.0,
    'pulmonary': 1.0,
    'dermatological': 0.75,
    'mucosal': 0.25,
    'rheumatic': 0.5,
    'cycle_phase': 0.0,  # disabled: no predictive signal in data (Fisher p>0.2, OR inverted)
    # Multi-day predictors
    'symptom_burden_weight': 1.0,
    'rmssd_deviation_weight': 0.5,  # speculative, conservative
    'rmssd_instability_weight': 0.5,  # new, pre-flare |ΔRMSSD| surge — starts conservative pending validation
    'resp_rate_deviation_weight': 0.5,  # speculative, conservative
    # Category multipliers
    'uv_weight': 1.0,
    'exertion_weight': 1.0,
    'temperature_weight': 1.0,
    'pain_fatigue_weight': 1.0,
    # Threshold
    'flare_threshold': 8.0,
}

# Symptom flag → per-symptom notes column, for severity-vocab tiering.
# Rheumatic is handled separately (joint tiering stays in place).
SYMPTOM_NOTES_COLUMN = {
    'neurological': 'neuro_notes',
    'cognitive': 'cognitive_notes',
    'musculature': 'musculature_notes',
    'migraine': 'migraine_notes',
    'pulmonary': 'pulmonary_notes',
    'dermatological': 'derm_notes',
    'mucosal': 'mucosal_notes',
}


def symptom_points(symptom, obs, baseline_weight):
    """Return the score contribution for a symptom. When the notes contain
    severity vocabulary, use the tier-based points (mild=1.0, major=1.5,
    extreme=2.0); otherwise fall back to baseline_weight, preserving prior
    behavior for bland notes / empty notes / flag-only days."""
    if not obs.get(symptom):
        return 0.0
    notes = obs.get(SYMPTOM_NOTES_COLUMN.get(symptom, '')) or ''
    tier_pts = severity_score(notes)
    return tier_pts if tier_pts is not None else baseline_weight


# Path to custom weights config
CUSTOM_WEIGHTS_PATH = os.path.join(DATA_DIR, 'config', 'custom_weights.json')

def get_current_weights(user_id=None):
    """
    Load weights from user preferences if available, then filesystem fallback,
    otherwise return defaults.
    """
    # Try user preferences first (Phase 2+)
    if user_id is not None:
        prefs = db.get_user_preferences(user_id)
        if prefs and prefs.get('custom_weights'):
            try:
                custom = json.loads(prefs['custom_weights'])
                weights = DEFAULT_WEIGHTS.copy()
                weights.update(custom)
                return weights
            except (json.JSONDecodeError, TypeError):
                pass

    # Fallback to filesystem (pre-migration compatibility)
    if os.path.exists(CUSTOM_WEIGHTS_PATH):
        try:
            with open(CUSTOM_WEIGHTS_PATH, 'r') as f:
                custom = json.load(f)
                weights = DEFAULT_WEIGHTS.copy()
                weights.update(custom)
                return weights
        except Exception as e:
            print(f"Error loading custom weights: {e}")
            return DEFAULT_WEIGHTS.copy()
    return DEFAULT_WEIGHTS.copy()

def save_custom_weights(weights, user_id=None):
    """
    Save custom weights. Writes to user_preferences if user_id provided,
    otherwise falls back to filesystem.
    """
    if user_id is not None:
        db.upsert_user_preferences(user_id, {
            'custom_weights': json.dumps(weights)
        })
        return

    # Filesystem fallback
    config_dir = os.path.dirname(CUSTOM_WEIGHTS_PATH)
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)
    with open(CUSTOM_WEIGHTS_PATH, 'w') as f:
        json.dump(weights, f, indent=2)

def reset_to_default_weights(user_id=None):
    """
    Reset weights to defaults. Clears from user_preferences if user_id provided.
    """
    if user_id is not None:
        db.upsert_user_preferences(user_id, {'custom_weights': None})
        return
    if os.path.exists(CUSTOM_WEIGHTS_PATH):
        os.remove(CUSTOM_WEIGHTS_PATH)
        
def calculate_flare_score_with_weights(obs, weights):
    """Calculate score using custom weights — delegates to full scoring function."""
    return calculate_flare_prime_score(obs, weights_override=weights)


# The only valid flare severities. Anything else from a form is dropped to
# None before storage — the value later renders into the portal's burden
# chart, so keeping it to a known set is defense-in-depth against injection.
VALID_FLARE_SEVERITIES = ("minor", "major", "er_visit")


# ============================================================
# Timeline
# ============================================================

def _score_components(obs: dict, user_id: int | None = None) -> dict:
    """Compute per-category score contributions for a single observation.

    Returns a dict with named component scores that sum to the total flare
    prime score. This is the single source of truth for score attribution —
    uses the same logic as calculate_flare_prime_score().

    user_id: whose weights/preferences to score with. Defaults to the
    logged-in user; the clinician portal passes the link owner explicitly
    since portal requests carry no session.
    """
    if user_id is None and current_user.is_authenticated:
        user_id = current_user.id
    weights = get_current_weights(user_id)
    uv_w = weights.get('uv_weight', 1.0)
    exertion_w = weights.get('exertion_weight', 1.0)
    temp_w = weights.get('temperature_weight', 1.0)
    pf_w = weights.get('pain_fatigue_weight', 1.0)

    c = {}

    # UV dose + cumulative
    sun_min = obs.get('sun_exposure_min') or 0
    uv_row = obs.get('_uv_row')
    protection = UV_PROTECTION_MULTIPLIERS.get(obs.get('uv_protection_level') or 'none', 1.0)
    w_uv = weighted_uv(uv_row)
    uv_dose = (w_uv ** 1.5) * sun_min * protection
    uv_pts = 0
    if uv_dose >= 800:
        uv_pts = 3 * uv_w
    elif uv_dose >= 400:
        uv_pts = 1.25 * uv_w
    cum_uv = obs.get('_cumulative_uv_dose')
    if cum_uv is not None and cum_uv >= 2500:
        uv_pts += 1.5 * uv_w
    elif cum_uv is not None and cum_uv >= 1500:
        uv_pts += 0.75 * uv_w
    c['uv'] = round(uv_pts, 2)

    # Exertion
    steps = obs.get('steps') or 0
    hours_slept = obs.get('hours_slept') or 8
    steps_baseline = obs.get('_steps_baseline')
    if steps_baseline is None:
        try:
            _p = db.get_user_preferences(user_id) if user_id else {}
            steps_baseline = _p.get('steps_baseline') if _p else None
        except Exception:
            steps_baseline = None
    ex_pts = 0
    if steps_baseline and steps_baseline > 0 and steps > 0:
        overexertion = (steps / steps_baseline) * (8.0 / max(hours_slept, 1))
        if overexertion >= 1.8:
            ex_pts = 2.0 * exertion_w
        elif overexertion >= 1.4:
            ex_pts = 1.5 * exertion_w
    elif hours_slept > 0:
        ratio = steps / hours_slept
        if ratio >= 2000:
            ex_pts = 2.0 * exertion_w
        elif ratio >= 1500:
            ex_pts = 1.5 * exertion_w
    c['exertion'] = round(ex_pts, 2)

    # Temperature
    basal_temp = obs.get('basal_temp_delta') or 0
    t_pts = 0
    if basal_temp >= 0.8:
        t_pts = 3 * temp_w
    elif basal_temp >= 0.5:
        t_pts = 2 * temp_w
    elif basal_temp >= 0.3:
        t_pts = 1 * temp_w
    c['temperature'] = round(t_pts, 2)

    # Individual symptoms — tier-scored from notes vocab when present,
    # otherwise the per-symptom baseline weight (see symptom_points).
    sym_pts = 0
    for sym in ('neurological', 'cognitive', 'musculature', 'migraine',
                'pulmonary', 'dermatological', 'mucosal'):
        sym_pts += symptom_points(sym, obs, weights.get(sym, 0))
    if obs.get('rheumatic'):
        rheum_notes = (obs.get('rheumatic_notes') or '').lower()
        major_joints = ['hip', 'knee', 'shoulder', 'elbow', 'ankle', 'wrist', 'jaw']
        minor_joints = ['finger', 'toe', 'hand']
        if any(j in rheum_notes for j in major_joints):
            sym_pts += 2.0
        elif any(j in rheum_notes for j in minor_joints):
            sym_pts += 1.0
        else:
            sym_pts += weights.get('rheumatic', 0.5)
    c['symptoms'] = round(sym_pts, 2)

    # Pain & fatigue & emotional (laddered to match calculate_flare_prime_score)
    pf_pts = 0
    pain = obs.get('pain_scale') or 0
    fatigue = obs.get('fatigue_scale') or 0
    emotional = obs.get('emotional_state') or 5
    if pain >= 7:
        pf_pts += 3.5 * pf_w
    elif pain >= 6:
        pf_pts += 2.5 * pf_w
    elif pain >= 5:
        pf_pts += 1.5 * pf_w
    elif pain >= 4:
        pf_pts += 0.5 * pf_w
    if fatigue >= 7:
        pf_pts += 3.5 * pf_w
    elif fatigue >= 6:
        pf_pts += 2.5 * pf_w
    elif fatigue >= 5:
        pf_pts += 1.5 * pf_w
    elif fatigue >= 4:
        pf_pts += 0.5 * pf_w
    if emotional <= 4:
        pf_pts += 2 * pf_w
    c['pain_fatigue'] = round(pf_pts, 2)

    # Symptom burden delta
    burden_w = weights.get('symptom_burden_weight', 1.0)
    burden_delta = obs.get('_symptom_burden_delta')
    b_pts = 0
    if burden_delta is not None:
        if burden_delta >= 3.0:
            b_pts = 3.0 * burden_w
        elif burden_delta >= 2.0:
            b_pts = 2.0 * burden_w
        elif burden_delta >= 1.0:
            b_pts = 1.0 * burden_w
    c['burden_delta'] = round(b_pts, 2)

    # RMSSD deviation
    rmssd_w = weights.get('rmssd_deviation_weight', 0.5)
    rmssd_dev = obs.get('_rmssd_deviation')
    r_pts = 0
    if rmssd_dev is not None:
        if rmssd_dev <= -25:
            r_pts = 1.5 * rmssd_w
        elif rmssd_dev <= -15:
            r_pts = 0.75 * rmssd_w
    c['rmssd'] = round(r_pts, 2)

    # RMSSD instability
    inst_w = weights.get('rmssd_instability_weight', 0.5)
    rmssd_inst = obs.get('_rmssd_instability')
    i_pts = 0
    if rmssd_inst is not None:
        if rmssd_inst >= 50:
            i_pts = 1.5 * inst_w
        elif rmssd_inst >= 25:
            i_pts = 0.75 * inst_w
    c['rmssd_instability'] = round(i_pts, 2)

    # Respiratory rate deviation
    resp_w = weights.get('resp_rate_deviation_weight', 0.5)
    resp_dev = obs.get('_resp_rate_deviation')
    rr_pts = 0
    if resp_dev is not None:
        if resp_dev >= 15:
            rr_pts = 1.5 * resp_w
        elif resp_dev >= 10:
            rr_pts = 0.75 * resp_w
    c['resp_rate'] = round(rr_pts, 2)

    c['total'] = round(sum(c.values()), 1)
    return c


def _detect_ovulation_bbt(bbt_by_date: dict, cycle_start: date, cycle_end: date):
    """Detect ovulation from biphasic BBT shift within a cycle window.

    Collects non-null BBT readings in [cycle_start, cycle_end), requires >=8 data points.
    Computes a follicular-phase average from the first 5 readings, then finds the first
    date of a 3-consecutive-day sustained rise >= 0.1 deg F above that average.
    Returns the first day of the sustained rise, or None if pattern not found.
    """
    readings = []
    d = cycle_start
    while d < cycle_end:
        bbt = bbt_by_date.get(d.isoformat())
        if bbt is not None:
            readings.append((d, bbt))
        d += timedelta(days=1)

    if len(readings) < 8:
        return None

    follicular_avg = sum(v for _, v in readings[:5]) / 5
    threshold = follicular_avg + 0.1

    consecutive = 0
    first_high = None
    for d, bbt in readings[5:]:
        if bbt >= threshold:
            consecutive += 1
            if first_high is None:
                first_high = d
            if consecutive >= 3:
                return first_high
        else:
            consecutive = 0
            first_high = None
    return None


def _detect_period_starts(sorted_obs: list) -> list[str]:
    """Detect period start dates from sorted observations.

    Rules:
    - Period starts on first day of non-spotting flow (or spotting that
      escalates to non-spotting within 2 days — retroactive start).
    - Period stays open for a minimum of 3 days after start.
    - Period closes only when 3 consecutive days have no flow logged
      (missing days count as "unknown", not "no flow").
    """
    period_starts = []
    period_start_date = None
    last_flow_date = None

    obs_by_date = {r['date']: r for r in sorted_obs}

    for row in sorted_obs:
        d = date.fromisoformat(row['date'])
        flow = row.get('period_flow') or ''
        has_real_flow = flow in ('light', 'medium', 'heavy')
        has_spotting = flow == 'spotting'
        has_any_flow = has_real_flow or has_spotting

        if period_start_date is None:
            if has_real_flow:
                period_start_date = d
                last_flow_date = d
                # Retroactive: check if preceding days were spotting
                for lookback in (1, 2):
                    prev = (d - timedelta(days=lookback)).isoformat()
                    prev_obs = obs_by_date.get(prev)
                    if prev_obs and prev_obs.get('period_flow') == 'spotting':
                        period_start_date = d - timedelta(days=lookback)
                    else:
                        break
                period_starts.append(period_start_date.isoformat())
        else:
            # Check gap BEFORE updating last_flow_date so that a new
            # period's flow day can still satisfy the 3-day-gap close
            days_since_start = (d - period_start_date).days
            days_since_flow = (d - last_flow_date).days
            if days_since_start >= 3 and days_since_flow >= 3:
                period_start_date = None
                last_flow_date = None
                if has_real_flow:
                    period_start_date = d
                    last_flow_date = d
                    period_starts.append(d.isoformat())
            elif has_any_flow:
                last_flow_date = d

    return period_starts
    
    
    

# ============================================================
# Forecast Laboratory Helpers
# ============================================================

def _compute_phase_by_date_from_obs(all_obs: list) -> dict:
    """Build {date_str: 'pms'|'luteal'} from obs list using same logic as cycle_view.
    Returns {} if track_cycle is False or insufficient cycle data.
    """
    prefs = get_user_prefs() if current_user and current_user.is_authenticated else {}
    if not prefs.get('track_cycle', CONFIG.get('track_cycle')):
        return {}

    sorted_obs = sorted(all_obs, key=lambda r: r['date'])
    bbt_by_date = {
        r['date']: r['basal_temp_delta']
        for r in sorted_obs
        if r.get('basal_temp_delta') is not None
    }

    # Detect period starts (3-day min, spotting retroactive, 3-day gap to close)
    period_starts: list = _detect_period_starts(sorted_obs)

    if len(period_starts) < 2:
        return {}

    lengths_raw = [
        (date.fromisoformat(period_starts[i + 1]) - date.fromisoformat(period_starts[i])).days
        for i in range(len(period_starts) - 1)
    ]
    lengths = [l for l in lengths_raw if l <= 90]
    recent = lengths[-6:] if lengths else []
    avg_cycle = round(sum(recent) / len(recent)) if recent else 28

    phase_by_date: dict = {}
    for i, start_str in enumerate(period_starts):
        cycle_start = date.fromisoformat(start_str)
        cycle_end = (
            date.fromisoformat(period_starts[i + 1])
            if i + 1 < len(period_starts)
            else cycle_start + timedelta(days=avg_cycle)
        )
        detected_ov = _detect_ovulation_bbt(bbt_by_date, cycle_start, cycle_end)
        lut = detected_ov if detected_ov else cycle_end - timedelta(days=14)
        pms = lut + timedelta(days=7)
        d = lut
        while d < cycle_end:
            phase_by_date[d.isoformat()] = 'pms' if d >= pms else 'luteal'
            d += timedelta(days=1)

    return phase_by_date


def _inject_cycle_phase(obs_list: list) -> None:
    """Annotate obs dicts in-place with cycle_in_high_risk_phase and cycle_phase_name."""
    prefs = get_user_prefs() if current_user and current_user.is_authenticated else {}
    if not prefs.get('track_cycle', CONFIG.get('track_cycle')):
        return
    phase_by_date = _compute_phase_by_date_from_obs(obs_list)
    for obs in obs_list:
        phase = phase_by_date.get(obs['date'])
        obs['cycle_in_high_risk_phase'] = phase in ('pms', 'luteal')
        obs['cycle_phase_name'] = phase


def _compute_bbt_hint(user_id: int) -> Optional[dict]:
    """Return recent BBT stats for display near the BBT entry field.
    Shows 6-week rolling follicular and luteal averages so users can calibrate.
    Returns {follicular_avg, luteal_avg, n_readings} or None if < 3 follicular readings.
    """
    all_obs = db.get_all_daily_observations(user_id)
    if not all_obs:
        return None
    all_obs.sort(key=lambda r: r['date'])
    phase_by_date = _compute_phase_by_date_from_obs(all_obs)

    cutoff = (date.today() - timedelta(days=42)).isoformat()
    foll_vals = []
    lut_vals = []
    for obs in all_obs:
        if obs['date'] < cutoff:
            continue
        bbt = obs.get('basal_temp_delta')
        if bbt is None:
            continue
        phase = phase_by_date.get(obs['date'])
        if phase in ('pms', 'luteal'):
            lut_vals.append(bbt)
        else:
            foll_vals.append(bbt)

    if len(foll_vals) < 3:
        return None

    return {
        'follicular_avg': round(sum(foll_vals) / len(foll_vals), 2),
        'luteal_avg': round(sum(lut_vals) / len(lut_vals), 2) if lut_vals else None,
        'n_readings': len(foll_vals) + len(lut_vals),
    }


def calculate_flare_prime_score(obs, weights_override=None):
    """
    Calculate flare prime score for a single observation.
    Based on refined logic with exponential UV weighting.

    Args:
        obs: daily observation dict
        weights_override: optional dict to override stored weights (used by simulation)

    Weights can be customized via Forecast Lab (/forecast/lab)
    """
    score = 0.0

    # Load current weights (from user prefs or defaults), apply overrides
    if weights_override:
        weights = DEFAULT_WEIGHTS.copy()
        weights.update(weights_override)
    else:
        weights = get_current_weights(current_user.id if current_user.is_authenticated else None)

    # Category multipliers (default 1.0 = no change)
    uv_w = weights.get('uv_weight', 1.0)
    exertion_w = weights.get('exertion_weight', 1.0)
    temp_w = weights.get('temperature_weight', 1.0)
    pf_w = weights.get('pain_fatigue_weight', 1.0)

    # 1. UV Dose (weighted UV × sun minutes × protection factor)
    sun_min = obs.get('sun_exposure_min') or 0
    uv_row = obs.get('_uv_row')
    if uv_row is None and obs.get('date'):
        # Auto-lookup UV data if not pre-injected
        try:
            user_id = current_user.id if current_user.is_authenticated else None
            _prefs = db.get_user_preferences(user_id) if user_id else {}
            _loc = db.make_location_key(
                _prefs.get('location_lat') or CONFIG.get('location_lat', 0),
                _prefs.get('location_lon') or CONFIG.get('location_lon', 0),
            ) if _prefs else 'default'
            uv_row = db.get_uv_data(_loc, obs['date'])
        except Exception:
            uv_row = None
    protection = UV_PROTECTION_MULTIPLIERS.get(
        obs.get('uv_protection_level') or 'none', 1.0)
    w_uv = weighted_uv(uv_row)
    uv_dose = (w_uv ** 1.5) * sun_min * protection
    if uv_dose >= 800:
        score += 3 * uv_w
    elif uv_dose >= 400:
        score += 1.25 * uv_w

    # Cumulative UV load bonus (prior 4 days, decay-weighted 0.8/0.6/0.4/0.2)
    # Thresholds scaled 1.5x from old 3-day window to account for extended lookback.
    cum_uv = obs.get('_cumulative_uv_dose')
    if cum_uv is not None and cum_uv >= 2500:
        score += 1.5 * uv_w
    elif cum_uv is not None and cum_uv >= 1500:
        score += 0.75 * uv_w

    # 2. Physical Overexertion (steps / hours slept)
    steps = obs.get('steps') or 0
    hours_slept = obs.get('hours_slept') or 8
    steps_baseline = obs.get('_steps_baseline')
    if steps_baseline is None:
        try:
            _uid = current_user.id if current_user.is_authenticated else None
            _p = db.get_user_preferences(_uid) if _uid else {}
            steps_baseline = _p.get('steps_baseline') if _p else None
        except Exception:
            steps_baseline = None

    if steps_baseline and steps_baseline > 0 and steps > 0:
        overexertion = (steps / steps_baseline) * (8.0 / max(hours_slept, 1))
        if overexertion >= 1.8:
            score += 2.0 * exertion_w
        elif overexertion >= 1.4:
            score += 1.5 * exertion_w
    elif hours_slept > 0:
        exertion_ratio = steps / hours_slept
        if exertion_ratio >= 2000:
            score += 2.0 * exertion_w
        elif exertion_ratio >= 1500:
            score += 1.5 * exertion_w

    # 3. Basal Temperature (simplified, non-overlapping)
    basal_temp = obs.get('basal_temp_delta') or 0
    if basal_temp >= 0.8:
        score += 3 * temp_w
    elif basal_temp >= 0.5:
        score += 2 * temp_w
    elif basal_temp >= 0.3:
        score += 1 * temp_w
    
    # 4. Symptoms — tier-scored from notes vocab when present, otherwise the
    # per-symptom baseline weight (see symptom_points at top of file).
    for sym in ('neurological', 'cognitive', 'musculature', 'migraine',
                'pulmonary', 'dermatological', 'mucosal'):
        score += symptom_points(sym, obs, weights[sym])

    # 5. Rheumatic (parse notes for joint type)
    if obs.get('rheumatic'):
        rheum_notes = (obs.get('rheumatic_notes') or '').lower()
        major_joints = ['hip', 'knee', 'shoulder', 'elbow', 'ankle', 'wrist', 'jaw']
        minor_joints = ['finger', 'toe', 'hand']
        
        if any(joint in rheum_notes for joint in major_joints):
            score += 2.0
        elif any(joint in rheum_notes for joint in minor_joints):
            score += 1.0
        else:
            score += weights['rheumatic']
    
    # 6. Pain Scale (laddered — pain is a strong severity axis, d=+1.01 vs baseline)
    # Previous cliff at >=7 only fired on 12% of flare days. Data shows >=4
    # already discriminates 75% flare vs 5% non-flare.
    pain = obs.get('pain_scale') or 0
    if pain >= 7:
        score += 3.5 * pf_w
    elif pain >= 6:
        score += 2.5 * pf_w
    elif pain >= 5:
        score += 1.5 * pf_w
    elif pain >= 4:
        score += 0.5 * pf_w

    # 7. Fatigue Scale (laddered to match pain, d=+0.83 vs baseline)
    fatigue = obs.get('fatigue_scale') or 0
    if fatigue >= 7:
        score += 3.5 * pf_w
    elif fatigue >= 6:
        score += 2.5 * pf_w
    elif fatigue >= 5:
        score += 1.5 * pf_w
    elif fatigue >= 4:
        score += 0.5 * pf_w

    # 8. Emotional State
    emotional = obs.get('emotional_state') or 5
    if emotional <= 4:
        score += 2 * pf_w

    # 9. Cycle phase (PMS/luteal risk elevation)
    if obs.get('cycle_in_high_risk_phase'):
        score += weights.get('cycle_phase', 1.0)

    # 10. Symptom burden delta (acceleration above personal baseline)
    burden_w = weights.get('symptom_burden_weight', 1.0)
    burden_delta = obs.get('_symptom_burden_delta')
    if burden_delta is not None:
        if burden_delta >= 3.0:
            score += 3.0 * burden_w
        elif burden_delta >= 2.0:
            score += 2.0 * burden_w
        elif burden_delta >= 1.0:
            score += 1.0 * burden_w

    # 11. RMSSD baseline deviation (vagal withdrawal signal, d=-0.35)
    rmssd_w = weights.get('rmssd_deviation_weight', 0.5)
    rmssd_dev = obs.get('_rmssd_deviation')
    if rmssd_dev is not None:
        if rmssd_dev <= -25:
            score += 1.5 * rmssd_w
        elif rmssd_dev <= -15:
            score += 0.75 * rmssd_w

    # 11b. RMSSD instability — mean |ΔRMSSD| in prior 5 days vs 30-day baseline.
    # Captures autonomic chaos (oscillation) separately from level-based withdrawal.
    # Independent signal — can fire alongside _rmssd_deviation.
    inst_w = weights.get('rmssd_instability_weight', 0.5)
    rmssd_inst = obs.get('_rmssd_instability')
    if rmssd_inst is not None:
        if rmssd_inst >= 50:
            score += 1.5 * inst_w
        elif rmssd_inst >= 25:
            score += 0.75 * inst_w

    # 12. Respiratory rate baseline deviation (pre-event elevation signal)
    resp_w = weights.get('resp_rate_deviation_weight', 0.5)
    resp_dev = obs.get('_resp_rate_deviation')
    if resp_dev is not None:
        if resp_dev >= 15:
            score += 1.5 * resp_w
        elif resp_dev >= 10:
            score += 0.75 * resp_w

    return round(score, 1)

def get_risk_level(score, threshold=8.0):
    """Determine risk level based on score.
    Breakpoints scale proportionally with the flare threshold.
    """
    moderate = threshold * 0.625   # default 5.0
    high = threshold               # default 8.0
    critical = threshold * 1.5     # default 12.0

    if score < moderate:
        return {
            'level': 'Low Risk',
            'color': '#4a9e6e',
            'description': 'Your flare risk is low. Keep up your current routine and rest patterns.'
        }
    elif score < high:
        return {
            'level': 'Moderate Risk',
            'color': '#d4b84a',
            'description': 'Elevated risk detected. Consider reducing physical demands and UV exposure.'
        }
    elif score < critical:
        return {
            'level': 'High Risk',
            'color': '#d4784a',
            'description': 'High flare risk. Prioritize rest, avoid sun exposure, and monitor symptoms closely.'
        }
    else:
        return {
            'level': 'Critical Risk',
            'color': '#c94040',
            'description': 'Critical flare risk. Consider a rest day and avoid all triggering activities.'
        }


def get_contributing_factors(obs: dict) -> list:
        """Identify what's contributing to today's risk score."""
        factors = []
        
        # UV exposure (weighted dose with protection)
        sun_min = obs.get('sun_exposure_min') or 0
        uv_row = obs.get('_uv_row')
        if uv_row is None and obs.get('date'):
            try:
                _uid = current_user.id if current_user and current_user.is_authenticated else None
                _prefs = db.get_user_preferences(_uid) if _uid else {}
                _loc = db.make_location_key(
                    _prefs.get('location_lat') or CONFIG.get('location_lat', 0),
                    _prefs.get('location_lon') or CONFIG.get('location_lon', 0),
                ) if _prefs else 'default'
                uv_row = db.get_uv_data(_loc, obs['date'])
            except Exception:
                uv_row = None
        protection = UV_PROTECTION_MULTIPLIERS.get(
            obs.get('uv_protection_level', 'none'), 1.0)
        w_uv = weighted_uv(uv_row)
        uv_dose = (w_uv ** 1.5) * sun_min * protection
        prot_label = obs.get('uv_protection_level') or 'none'
        if uv_dose >= 800:
            factors.append({'name': f'High UV dose ({prot_label})', 'points': 3, 'color': '#d4b84a'})
        elif uv_dose >= 400:
            factors.append({'name': f'Moderate UV dose ({prot_label})', 'points': 1.25, 'color': '#d4b84a'})
        
        # Overexertion
        steps = obs.get('steps') or 0
        hours_slept = obs.get('hours_slept') or 8
        if hours_slept > 0:
            exertion_ratio = steps / hours_slept
            if exertion_ratio >= 2000:
                factors.append({'name': 'Severe overexertion', 'points': 2, 'color': '#c94040'})
            elif exertion_ratio >= 1500:
                factors.append({'name': 'Moderate overexertion', 'points': 1.5, 'color': '#d4784a'})
        
        # Temperature
        basal_temp = obs.get('basal_temp_delta') or 0
        if basal_temp >= 0.8:
            factors.append({'name': 'High fever', 'points': 3, 'color': '#c94040'})
        elif basal_temp >= 0.5:
            factors.append({'name': 'Moderate fever', 'points': 2, 'color': '#d4784a'})
        elif basal_temp >= 0.3:
            factors.append({'name': 'Mild fever', 'points': 1, 'color': '#d4b84a'})
        
        # Active symptoms
        if obs.get('migraine'):
            factors.append({'name': 'Migraine', 'points': 1, 'color': '#c94040'})
        if obs.get('pulmonary'):
            factors.append({'name': 'Pulmonary symptoms', 'points': 1, 'color': '#4ab8b8'})
        if obs.get('musculature'):
            factors.append({'name': 'Muscle symptoms', 'points': 1.5, 'color': '#d4a054'})  # CHANGED
        if obs.get('dermatological'):
            factors.append({'name': 'Skin symptoms', 'points': 0.75, 'color': '#d4784a'})
        if obs.get('cognitive'):
            factors.append({'name': 'Cognitive symptoms', 'points': 1.0, 'color': '#9b72cf'})  # CHANGED
        if obs.get('neurological'):
            factors.append({'name': 'Neurological symptoms', 'points': 1.5, 'color': '#4a90d9'})  # CHANGED
        if obs.get('mucosal'):
            factors.append({'name': 'Mucosal symptoms', 'points': 0.25, 'color': '#d4c4a0'})
        
        # Rheumatic
        if obs.get('rheumatic'):
            rheum_notes = (obs.get('rheumatic_notes') or '').lower()
            if any(j in rheum_notes for j in ['hip', 'knee', 'shoulder', 'elbow', 'ankle', 'wrist', 'jaw']):
                factors.append({'name': 'Major joint pain', 'points': 2, 'color': '#e85d9e'})
            elif any(j in rheum_notes for j in ['finger', 'toe', 'hand']):
                factors.append({'name': 'Minor joint pain', 'points': 1, 'color': '#e85d9e'})
            else:
                factors.append({'name': 'Rheumatic symptoms', 'points': 0.5, 'color': '#e85d9e'})
        
        # Fatigue (laddered)
        fatigue = obs.get('fatigue_scale') or 0
        if fatigue >= 7:
            factors.append({'name': 'Severe fatigue', 'points': 3.5, 'color': '#d4a054'})
        elif fatigue >= 6:
            factors.append({'name': 'High fatigue', 'points': 2.5, 'color': '#d4a054'})
        elif fatigue >= 5:
            factors.append({'name': 'Moderate fatigue', 'points': 1.5, 'color': '#d4a054'})
        elif fatigue >= 4:
            factors.append({'name': 'Mild fatigue', 'points': 0.5, 'color': '#d4a054'})

        # Pain (laddered)
        pain = obs.get('pain_scale') or 0
        if pain >= 7:
            factors.append({'name': 'Severe pain', 'points': 3.5, 'color': '#c94040'})
        elif pain >= 6:
            factors.append({'name': 'High pain', 'points': 2.5, 'color': '#c94040'})
        elif pain >= 5:
            factors.append({'name': 'Moderate pain', 'points': 1.5, 'color': '#c94040'})
        elif pain >= 4:
            factors.append({'name': 'Mild pain', 'points': 0.5, 'color': '#c94040'})
        
        # Low emotional state
        emotional = obs.get('emotional_state') or 5
        if emotional <= 3:
            factors.append({'name': 'Low emotional state', 'points': 2, 'color': '#7a8499'})

        # Cycle phase
        if obs.get('cycle_in_high_risk_phase'):
            phase_label = 'PMS phase' if obs.get('cycle_phase_name') == 'pms' else 'Luteal phase'
            uid = current_user.id if current_user and current_user.is_authenticated else None
            cycle_weight = get_current_weights(uid).get('cycle_phase', 1.0)
            factors.append({'name': phase_label, 'points': cycle_weight, 'color': '#9563ec'})

        # RMSSD instability (autonomic chaos, independent from level)
        rmssd_inst = obs.get('_rmssd_instability')
        if rmssd_inst is not None:
            if rmssd_inst >= 50:
                factors.append({'name': 'Severe RMSSD instability', 'points': 1.5, 'color': '#c084fc'})
            elif rmssd_inst >= 25:
                factors.append({'name': 'Elevated RMSSD instability', 'points': 0.75, 'color': '#c084fc'})

        return factors
