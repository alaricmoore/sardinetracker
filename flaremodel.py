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

# Score categories, in the order the charts stack them.
_SCORE_CATEGORIES = ('uv', 'exertion', 'temperature', 'symptoms', 'pain_fatigue',
                     'cycle', 'burden_delta', 'rmssd', 'rmssd_instability', 'resp_rate')

# Symptom flag -> the name and colour it is listed under.
_SYMPTOM_FACTORS = (
    ('neurological', 'Neurological symptoms', '#4a90d9'),
    ('cognitive', 'Cognitive symptoms', '#9b72cf'),
    ('musculature', 'Muscle symptoms', '#d4a054'),
    ('migraine', 'Migraine', '#c94040'),
    ('pulmonary', 'Pulmonary symptoms', '#4ab8b8'),
    ('dermatological', 'Skin symptoms', '#d4784a'),
    ('mucosal', 'Mucosal symptoms', '#d4c4a0'),
)


def _ladder(value):
    """Pain and fatigue share one ladder: (label, base points)."""
    if value >= 7:
        return 'Severe', 3.5
    if value >= 6:
        return 'High', 2.5
    if value >= 5:
        return 'Moderate', 1.5
    if value >= 4:
        return 'Mild', 0.5
    return None, 0


def _score_items(obs: dict, user_id: int | None = None,
                 weights: dict | None = None) -> list:
    """Every individual contribution to the flare prime score for one day.

    Returns [{'category', 'name', 'points', 'color'}, ...] for each rule that
    fired, with unrounded points. This is the one implementation of the
    scoring rules: _score_components adds the items up by category, and its
    total is the flare prime score; get_contributing_factors lists them. So
    the score, its breakdown and its explanation cannot disagree.

    user_id: whose weights/preferences to score with. Defaults to the
    logged-in user; the clinician portal passes the link owner explicitly
    since portal requests carry no session.
    weights: a full weights dict to score with instead of the stored ones
    (the Forecast Lab simulation passes one).
    """
    if user_id is None and current_user.is_authenticated:
        user_id = current_user.id
    if weights is None:
        weights = get_current_weights(user_id)
    uv_w = weights.get('uv_weight', 1.0)
    exertion_w = weights.get('exertion_weight', 1.0)
    temp_w = weights.get('temperature_weight', 1.0)
    pf_w = weights.get('pain_fatigue_weight', 1.0)

    items = []

    def add(category, name, points, color):
        if points:
            items.append({'category': category, 'name': name, 'points': points, 'color': color})

    # UV dose (weighted UV x sun minutes x protection factor) + cumulative load
    sun_min = obs.get('sun_exposure_min') or 0
    uv_row = obs.get('_uv_row')
    if uv_row is None and obs.get('date'):
        # Auto-lookup UV data if not pre-injected
        try:
            _prefs = db.get_user_preferences(user_id) if user_id else {}
            _loc = db.make_location_key(
                _prefs.get('location_lat') or CONFIG.get('location_lat', 0),
                _prefs.get('location_lon') or CONFIG.get('location_lon', 0),
            ) if _prefs else 'default'
            uv_row = db.get_uv_data(_loc, obs['date'])
        except Exception:
            uv_row = None
    protection_level = obs.get('uv_protection_level') or 'none'
    protection = UV_PROTECTION_MULTIPLIERS.get(protection_level, 1.0)
    uv_dose = (weighted_uv(uv_row) ** 1.5) * sun_min * protection
    if uv_dose >= 800:
        add('uv', f'High UV dose ({protection_level})', 3 * uv_w, '#d4b84a')
    elif uv_dose >= 400:
        add('uv', f'Moderate UV dose ({protection_level})', 1.25 * uv_w, '#d4b84a')
    # Prior 4 days, decay-weighted 0.8/0.6/0.4/0.2. Thresholds scaled 1.5x
    # from the old 3-day window to account for the extended lookback.
    cum_uv = obs.get('_cumulative_uv_dose')
    if cum_uv is not None and cum_uv >= 2500:
        add('uv', 'Heavy UV load over the past 4 days', 1.5 * uv_w, '#d4b84a')
    elif cum_uv is not None and cum_uv >= 1500:
        add('uv', 'UV load over the past 4 days', 0.75 * uv_w, '#d4b84a')

    # Physical overexertion: against the personal steps baseline when there
    # is one, otherwise steps per hour slept
    steps = obs.get('steps') or 0
    hours_slept = obs.get('hours_slept') or 8
    steps_baseline = obs.get('_steps_baseline')
    if steps_baseline is None:
        try:
            _p = db.get_user_preferences(user_id) if user_id else {}
            steps_baseline = _p.get('steps_baseline') if _p else None
        except Exception:
            steps_baseline = None
    exertion = None
    if steps_baseline and steps_baseline > 0 and steps > 0:
        overexertion = (steps / steps_baseline) * (8.0 / max(hours_slept, 1))
        if overexertion >= 1.8:
            exertion = 'severe'
        elif overexertion >= 1.4:
            exertion = 'moderate'
    elif hours_slept > 0:
        ratio = steps / hours_slept
        if ratio >= 2000:
            exertion = 'severe'
        elif ratio >= 1500:
            exertion = 'moderate'
    if exertion == 'severe':
        add('exertion', 'Severe overexertion', 2.0 * exertion_w, '#c94040')
    elif exertion == 'moderate':
        add('exertion', 'Moderate overexertion', 1.5 * exertion_w, '#d4784a')

    # Basal temperature (non-overlapping)
    basal_temp = obs.get('basal_temp_delta') or 0
    if basal_temp >= 0.8:
        add('temperature', 'High fever', 3 * temp_w, '#c94040')
    elif basal_temp >= 0.5:
        add('temperature', 'Moderate fever', 2 * temp_w, '#d4784a')
    elif basal_temp >= 0.3:
        add('temperature', 'Mild fever', 1 * temp_w, '#d4b84a')

    # Individual symptoms: tier-scored from notes vocab when present,
    # otherwise the per-symptom weight (see symptom_points)
    for sym, name, color in _SYMPTOM_FACTORS:
        add('symptoms', name, symptom_points(sym, obs, weights.get(sym, 0)), color)

    # Rheumatic: joint size parsed from the notes
    if obs.get('rheumatic'):
        rheum_notes = (obs.get('rheumatic_notes') or '').lower()
        if any(j in rheum_notes for j in ('hip', 'knee', 'shoulder', 'elbow', 'ankle', 'wrist', 'jaw')):
            add('symptoms', 'Major joint pain', 2.0, '#e85d9e')
        elif any(j in rheum_notes for j in ('finger', 'toe', 'hand')):
            add('symptoms', 'Minor joint pain', 1.0, '#e85d9e')
        else:
            add('symptoms', 'Rheumatic symptoms', weights.get('rheumatic', 0.5), '#e85d9e')

    # Pain and fatigue (laddered: pain >= 4 already discriminates 75% flare vs
    # 5% non-flare), and low emotional state
    label, pts = _ladder(obs.get('pain_scale') or 0)
    add('pain_fatigue', f'{label} pain', pts * pf_w, '#c94040')
    label, pts = _ladder(obs.get('fatigue_scale') or 0)
    add('pain_fatigue', f'{label} fatigue', pts * pf_w, '#d4a054')
    if (obs.get('emotional_state') or 5) <= 4:
        add('pain_fatigue', 'Low emotional state', 2 * pf_w, '#7a8499')

    # Cycle phase (PMS/luteal risk elevation)
    if obs.get('cycle_in_high_risk_phase'):
        phase = 'PMS phase' if obs.get('cycle_phase_name') == 'pms' else 'Luteal phase'
        add('cycle', phase, weights.get('cycle_phase', 1.0), '#9563ec')

    # Symptom burden delta (acceleration above personal baseline)
    burden_w = weights.get('symptom_burden_weight', 1.0)
    burden_delta = obs.get('_symptom_burden_delta')
    if burden_delta is not None:
        if burden_delta >= 3.0:
            add('burden_delta', 'Symptoms well above your baseline', 3.0 * burden_w, '#5b9bd5')
        elif burden_delta >= 2.0:
            add('burden_delta', 'Symptoms above your baseline', 2.0 * burden_w, '#5b9bd5')
        elif burden_delta >= 1.0:
            add('burden_delta', 'Symptoms slightly above your baseline', 1.0 * burden_w, '#5b9bd5')

    # RMSSD baseline deviation (vagal withdrawal signal)
    rmssd_w = weights.get('rmssd_deviation_weight', 0.5)
    rmssd_dev = obs.get('_rmssd_deviation')
    if rmssd_dev is not None:
        if rmssd_dev <= -25:
            add('rmssd', 'RMSSD well below baseline', 1.5 * rmssd_w, '#66bb6a')
        elif rmssd_dev <= -15:
            add('rmssd', 'RMSSD below baseline', 0.75 * rmssd_w, '#66bb6a')

    # RMSSD instability: day-to-day swings, independent of level
    inst_w = weights.get('rmssd_instability_weight', 0.5)
    rmssd_inst = obs.get('_rmssd_instability')
    if rmssd_inst is not None:
        if rmssd_inst >= 50:
            add('rmssd_instability', 'Severe RMSSD instability', 1.5 * inst_w, '#c084fc')
        elif rmssd_inst >= 25:
            add('rmssd_instability', 'Elevated RMSSD instability', 0.75 * inst_w, '#c084fc')

    # Respiratory rate baseline deviation (pre-event elevation signal)
    resp_w = weights.get('resp_rate_deviation_weight', 0.5)
    resp_dev = obs.get('_resp_rate_deviation')
    if resp_dev is not None:
        if resp_dev >= 15:
            add('resp_rate', 'Respiratory rate well above baseline', 1.5 * resp_w, '#e0a050')
        elif resp_dev >= 10:
            add('resp_rate', 'Respiratory rate above baseline', 0.75 * resp_w, '#e0a050')

    return items


def _score_components(obs: dict, user_id: int | None = None,
                      weights: dict | None = None) -> dict:
    """Per-category score contributions for a single observation.

    Returns each category's points plus 'total', which IS the flare prime
    score: calculate_flare_prime_score() returns this total, so the breakdown
    and the score cannot disagree. Parts are rounded to 2 places for display;
    the total is rounded once, from the unrounded parts. The rules themselves
    live in _score_items.
    """
    c = dict.fromkeys(_SCORE_CATEGORIES, 0)
    for item in _score_items(obs, user_id=user_id, weights=weights):
        c[item['category']] += item['points']
    # float() so a day with nothing firing scores 0.0, as it always has, not int 0.
    total = round(float(sum(c.values())), 1)
    c = {k: round(v, 2) for k, v in c.items()}
    c['total'] = total
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
    user_id = current_user.id if current_user.is_authenticated else None
    if weights_override:
        weights = DEFAULT_WEIGHTS.copy()
        weights.update(weights_override)
    else:
        weights = get_current_weights(user_id)
    # One implementation: the score is the breakdown's total, so the dashboard,
    # the forecast page and the clinician portal always show parts that add up
    # to the number the forecast and alerts use.
    return _score_components(obs, user_id=user_id, weights=weights)['total']

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
    """What is adding to today's flare score, largest first.

    Lists exactly the contributions that make up the score (see _score_items),
    each as {'name', 'points', 'color'}, so the points add up to the score.
    Callers that show "top factors" take the first few.
    """
    items = sorted(_score_items(obs), key=lambda i: -i['points'])
    return [{'name': i['name'], 'points': round(i['points'], 2), 'color': i['color']}
            for i in items]
