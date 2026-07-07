"""
biotracking app.py
------------------
Flask routes only. No database logic, no API calls.
All data access goes through db.py.
All UV fetching goes through uv_fetcher.py.

Run with:
    python app.py

Access locally:    http://localhost:5000
Access from phone: http://<your-mac-ip>:5000
"""

import calendar
import json
import math
import os
import statistics
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request, redirect, url_for, Response, session, send_from_directory

import bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

import db
import uv_fetcher
from severity_vocab import severity_score
import zipfile
import shutil
from pathlib import Path
from flask import send_file

from typing import Optional, Dict, List, Any
from collections import Counter

from apscheduler.schedulers.background import BackgroundScheduler


app = Flask(__name__)

# Apply any pending schema migrations. Idempotent — safe to call every startup.
# Prints a note only when something actually changed so logs stay quiet on
# no-op runs.
_migrations_applied = db.run_migrations()
if _migrations_applied:
    print(f"[db] applied {_migrations_applied} schema migration(s) at startup")

import os
import json

# ============================================================
# LAB ADJUSTMENTS
# ============================================================

# UV protection multipliers — applied to UV dose in scoring
UV_PROTECTION_MULTIPLIERS = {
    "none": 1.0,
    "spf_hat": 0.3,
    "full_cover": 0.1,
    "indoors_only": 0.0,
}


def weighted_uv(uv_row):
    """Compute weighted daily UV from morning/noon/evening readings."""
    if not uv_row:
        return 0.0
    m = float(uv_row.get("uv_morning") or 0)
    n = float(uv_row.get("uv_noon") or 0)
    e = float(uv_row.get("uv_evening") or 0)
    return m * 0.2 + n * 0.6 + e * 0.2


def compute_rmssd(rr_intervals: list) -> float | None:
    """Compute RMSSD from a list of RR intervals in milliseconds.
    RMSSD = sqrt(mean(successive_differences^2))
    Returns None if fewer than 2 intervals.
    """
    if len(rr_intervals) < 2:
        return None
    diffs = [rr_intervals[i + 1] - rr_intervals[i] for i in range(len(rr_intervals) - 1)]
    squared = [d * d for d in diffs]
    return round(math.sqrt(sum(squared) / len(squared)), 2)


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


_SYMPTOM_KEYS = [
    'neurological', 'cognitive', 'musculature', 'migraine',
    'pulmonary', 'dermatological', 'rheumatic', 'mucosal', 'gastro',
]


def _daily_symptom_count(obs: dict | None) -> int | None:
    """Count binary symptom flags for a single day's observation."""
    if not obs:
        return None
    return sum(1 for sym in _SYMPTOM_KEYS if obs.get(sym))


def _compute_symptom_burden_delta(obs_date: str, obs_by_date: dict) -> float | None:
    """Symptom burden as deviation from personal rolling baseline.

    Returns the delta between the 3-day recent average symptom count and
    the 14-day rolling baseline (days -17 through -4, avoiding pre-flare
    contamination). Positive = symptoms accelerating above normal.

    The baseline starts at day -4, not -3, so it does not share day -3 with
    the recent window (days -1..-3). Overlapping at -3 let the leading edge of
    the pre-flare symptom ramp inflate the baseline and shrink the delta it is
    meant to detect.
    Returns None if insufficient baseline data (< 7 days).
    """
    target = datetime.strptime(obs_date, "%Y-%m-%d").date()

    # 3-day recent window: days -1, -2, -3
    recent = []
    for offset in range(1, 4):
        d = (target - timedelta(days=offset)).isoformat()
        c = _daily_symptom_count(obs_by_date.get(d))
        if c is not None:
            recent.append(c)

    if not recent:
        return None

    # 14-day baseline window: days -17 through -4 (gap at -3 avoids sharing the
    # leading ramp day with the recent window)
    baseline = []
    for offset in range(4, 18):
        d = (target - timedelta(days=offset)).isoformat()
        c = _daily_symptom_count(obs_by_date.get(d))
        if c is not None:
            baseline.append(c)

    if len(baseline) < 7:
        return None

    recent_avg = sum(recent) / len(recent)
    baseline_avg = sum(baseline) / len(baseline)

    return round(recent_avg - baseline_avg, 2)


def _compute_rmssd_deviation(obs_date: str, obs_by_date: dict) -> float | None:
    """Percentage deviation of 7-day RMSSD average from 30-day baseline.

    Returns negative values when recent RMSSD is below baseline (vagal withdrawal).
    Returns None if insufficient data in either window.
    """
    target = datetime.strptime(obs_date, "%Y-%m-%d").date()

    # 7-day recent window (day-1 through day-7)
    recent = []
    for offset in range(1, 8):
        d = (target - timedelta(days=offset)).isoformat()
        obs = obs_by_date.get(d)
        if obs and obs.get('hrv_rmssd') is not None:
            recent.append(float(obs['hrv_rmssd']))

    # 30-day baseline window (day-8 through day-37, avoids overlap with recent)
    baseline = []
    for offset in range(8, 38):
        d = (target - timedelta(days=offset)).isoformat()
        obs = obs_by_date.get(d)
        if obs and obs.get('hrv_rmssd') is not None:
            baseline.append(float(obs['hrv_rmssd']))

    if len(recent) < 4 or len(baseline) < 4:
        return None

    recent_avg = sum(recent) / len(recent)
    baseline_avg = sum(baseline) / len(baseline)

    if baseline_avg == 0:
        return None

    return round((recent_avg - baseline_avg) / baseline_avg * 100, 1)


def _compute_rmssd_instability(obs_date: str, obs_by_date: dict) -> float | None:
    """Percentage deviation of recent day-to-day |ΔRMSSD| from a longer baseline.

    Captures autonomic *instability* (wild parasympathetic swings) rather than
    level-based withdrawal. Empirically, Alaric's major flares show their
    cleanest signature in this: day-to-day |ΔRMSSD| in the week before onset
    spikes well above her typical range, peaking at the day-1 → day-0 transition.
    This is a separate signal from _compute_rmssd_deviation and can fire alongside it.

    Recent: 5-day window (day-1 through day-5), yields 4 adjacent-day deltas.
    Baseline: 30-day window (day-6 through day-35), yields ~29 deltas — large
    enough to dilute post-flare-steroid oscillation days without skewing.

    Returns None if insufficient data in either window or baseline is zero.
    """
    target = datetime.strptime(obs_date, "%Y-%m-%d").date()

    def adjacent_deltas(start_offset: int, end_offset: int) -> list[float]:
        """Return |RMSSD[d] - RMSSD[d-1]| for days in [start_offset..end_offset]
        where both the day and the previous day have RMSSD values."""
        deltas = []
        for off in range(start_offset, end_offset + 1):
            d_curr = (target - timedelta(days=off)).isoformat()
            d_prev = (target - timedelta(days=off + 1)).isoformat()
            curr = obs_by_date.get(d_curr)
            prev = obs_by_date.get(d_prev)
            if curr and prev and curr.get('hrv_rmssd') is not None and prev.get('hrv_rmssd') is not None:
                deltas.append(abs(float(curr['hrv_rmssd']) - float(prev['hrv_rmssd'])))
        return deltas

    recent_deltas = adjacent_deltas(1, 5)
    baseline_deltas = adjacent_deltas(6, 35)

    if len(recent_deltas) < 3 or len(baseline_deltas) < 10:
        return None

    recent_mean = sum(recent_deltas) / len(recent_deltas)
    baseline_mean = sum(baseline_deltas) / len(baseline_deltas)

    if baseline_mean == 0:
        return None

    return round((recent_mean - baseline_mean) / baseline_mean * 100, 1)


def _compute_resp_rate_deviation(obs_date: str, obs_by_date: dict) -> float | None:
    """Percentage deviation of 3-day respiratory rate average from 14-day baseline.

    Returns positive values when recent respiratory rate is elevated.
    Returns None if insufficient data in either window.
    """
    target = datetime.strptime(obs_date, "%Y-%m-%d").date()

    # 3-day recent window (day-1 through day-3)
    recent = []
    for offset in range(1, 4):
        d = (target - timedelta(days=offset)).isoformat()
        obs = obs_by_date.get(d)
        if obs and obs.get('respiratory_rate') is not None:
            recent.append(float(obs['respiratory_rate']))

    # 14-day baseline window (day-4 through day-17, avoids pre-event contamination)
    baseline = []
    for offset in range(4, 18):
        d = (target - timedelta(days=offset)).isoformat()
        obs = obs_by_date.get(d)
        if obs and obs.get('respiratory_rate') is not None:
            baseline.append(float(obs['respiratory_rate']))

    if len(recent) < 2 or len(baseline) < 4:
        return None

    recent_avg = sum(recent) / len(recent)
    baseline_avg = sum(baseline) / len(baseline)

    if baseline_avg == 0:
        return None

    return round((recent_avg - baseline_avg) / baseline_avg * 100, 1)


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
CUSTOM_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), 'config', 'custom_weights.json')

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
    

# ============================================================
# FORECAST LAB MANUAL TEXT
# ============================================================

FORECAST_LAB_MANUAL ="""╔═══════════════════════════════════════════════════════════════════════════╗
║                    FLARE PREDICTION MODEL — USER MANUAL                   ║
╚═══════════════════════════════════════════════════════════════════════════╝

WHAT THIS IS
────────────
A transparent, statistical model for predicting lupus flare risk based on
daily observations. Unlike black-box AI, you can see exactly how it works
and tune it yourself.

HOW SCORING WORKS
─────────────────
Each day, the model computes a flare risk score by summing weighted
contributions from several categories. Before scoring, each observation
is enriched with multi-day context:

  _inject_scoring_context() pre-computes:
  • Cumulative UV dose from the prior 4 days (decay-weighted 0.8/0.6/0.4/0.2)
  • 3-day symptom burden (total symptom flags across days -1, -2, -3)
  • RMSSD baseline deviation (7-day rolling avg vs 30-day personal baseline)

These values are injected into each observation so the scoring function
has access to patterns that span multiple days, not just today's snapshot.

SCORING CATEGORIES
──────────────────

  1. UV Dose (weighted UV index^1.5 x sun minutes x protection factor)
     • Dose >= 800: +3.0 x uv_weight
     • Dose >= 400: +1.25 x uv_weight
     Cohen's d = +1.29, p < 0.0001 for 3-day cumulative sun exposure.

  2. Cumulative UV Load (prior 4 days, decay-weighted 0.8/0.6/0.4/0.2)
     • Cumulative >= 2500: +1.5 x uv_weight
     • Cumulative >= 1500: +0.75 x uv_weight
     Personal data analysis showed UV signal persists 2-4 days before major
     flares (unprotected ≥60 min: 79% at day-1, 67% at day-2/-3, 58% at day-4
     vs 35-40% non-flare baseline). Extended from prior 3-day window.

  3. Physical Overexertion (steps relative to baseline / sleep hours)
     • Overexertion ratio >= 1.8: +2.0 x exertion_weight
     • Overexertion ratio >= 1.4: +1.5 x exertion_weight

  4. Basal Temperature Delta (deviation from personal baseline)
     • Delta >= 0.8 F: +3.0 x temperature_weight
     • Delta >= 0.5 F: +2.0 x temperature_weight
     • Delta >= 0.3 F: +1.0 x temperature_weight

  5. Individual Symptoms (each adds its weight when flagged):
     • Neurological: 1.5
     • Cognitive: 1.0
     • Musculature: 1.5
     • Migraine: 1.0
     • Pulmonary: 1.0
     • Dermatological: 0.75
     • Mucosal: 0.25
     • Rheumatic: 0.5 base, 2.0 major joints, 1.0 minor joints

  6. Pain Scale (laddered — d=+1.01 vs non-flare baseline)
     Previous cliff at >=7 fired on only 12% of flare days. Data shows
     pain >=4 already discriminates 75% flare vs 5% non-flare.
     • Pain >= 7: +3.5 x pain_fatigue_weight
     • Pain >= 6: +2.5 x pain_fatigue_weight
     • Pain >= 5: +1.5 x pain_fatigue_weight
     • Pain >= 4: +0.5 x pain_fatigue_weight

  7. Fatigue Scale (laddered — d=+0.83 vs non-flare baseline)
     • Fatigue >= 7: +3.5 x pain_fatigue_weight
     • Fatigue >= 6: +2.5 x pain_fatigue_weight
     • Fatigue >= 5: +1.5 x pain_fatigue_weight
     • Fatigue >= 4: +0.5 x pain_fatigue_weight

  8. Emotional State
     • Emotional state <= 4: +2.0 x pain_fatigue_weight

  9. Cycle Phase
     • Weight set to 0.0 (disabled). Fisher exact tests showed no
       predictive signal (bleeding OR=0.70 p=0.24, PMS OR=1.12 p=0.70).
       With post-steroid cycles averaging 15.7 days vs the 28-day model
       assumption, 90% of days were flagged, adding constant bias.

  10. Symptom Burden Delta (acceleration above personal baseline)
      Raw symptom count saturates when you have chronic daily symptoms
      (e.g., neuro 76%, rheumatic 82%, derm 62% of days). What predicts
      a flare isn't having symptoms — it's having MORE than your usual
      number. The delta captures acceleration, not presence.

      Computation:
      • 3-day recent average: mean daily symptom count over days -1, -2, -3
      • 14-day rolling baseline: mean daily count over days -17 through -4
        (gap at day -3 avoids pre-flare ramp contaminating the baseline)
      • Delta = recent_avg - baseline_avg

      Scoring:
      • Delta >= 3.0: +3.0 x symptom_burden_weight (sharp acceleration)
      • Delta >= 2.0: +2.0 x symptom_burden_weight (moderate acceleration)
      • Delta >= 1.0: +1.0 x symptom_burden_weight (mild acceleration)
      • Delta < 1.0: no contribution (at or below baseline)

      Requires >= 7 days of baseline history; falls back to 0 if sparse.

  11. RMSSD Baseline Deviation — vagal withdrawal signal
      Compares 7-day rolling RMSSD average to 30-day personal baseline.
      Based on the cholinergic anti-inflammatory pathway: declining vagal
      tone weakens the inflammatory brake. Replicates Thanou 2016's within-
      patient ΔRMSSD-ΔSLEDAI finding (p=0.007). Post-bugfix rerun: pre-flare
      day-1/-2 Cohen's d = -0.28 all flares, -0.18 majors; on-flare-day
      RMSSD for majors drops ~46% from baseline. Underpowered cross-
      sectionally at n=8 majors but directionally consistent with lit.
      Default weight 0.5; Alaric currently tunes it to 1.25.
      • Deviation <= -25%: +1.5 x rmssd_deviation_weight
      • Deviation <= -15%: +0.75 x rmssd_deviation_weight

  11b. RMSSD Instability — day-to-day |ΔRMSSD| surge
      Compares mean |ΔRMSSD| in prior 5 days to a 30-day baseline.
      Captures autonomic *chaos* rather than level-based withdrawal.
      Independent signal from rule 11 — fires alongside it when both hold.
      Prototyped from the post-bugfix rerun analysis (rmssd_flare_rerun.py),
      which showed the surge/crash pattern is specific to MAJOR flares;
      minor flares show flatter trajectories.
      Personal data: day-1 → day-0 transition in majors averages ~120 ms
      |Δ| vs 60-70 ms baseline. Conservative weight (0.5) pending live
      validation.
      • Deviation >= 50%: +1.5 x rmssd_instability_weight
      • Deviation >= 25%: +0.75 x rmssd_instability_weight

  12. Respiratory Rate Baseline Deviation — pre-event elevation signal
      Compares 3-day rolling respiratory rate average to 14-day personal
      baseline (days -4 through -17, gap avoids pre-event contamination).
      Motivated by general critical-care deterioration literature (Barfod
      et al. 2017, OR=1.15 per breath/min, n=15,724) — NOT lupus-specific.
      Honest caveat: Alaric's cross-sectional pre-flare raw rate is
      weakly negative (d=-0.18 majors), opposite of the literature's
      elevated-rate prediction. The feature scores within-person deviation,
      which may still be testable on a per-event basis even when the
      group mean doesn't move. The /model resp-rate deviation chart (with
      dashed +10% / +15% threshold lines) is the live validation tool —
      watch whether the line crosses those thresholds 1-3 days before
      known flares. If consistently yes, raise the weight; if not, drop it.
      • Deviation >= 15%: +1.5 x resp_rate_deviation_weight
      • Deviation >= 10%: +0.75 x resp_rate_deviation_weight

  Threshold: 8.0 points = flare risk

UV LAG ANALYSIS — HOW IT WORKS
───────────────────────────────
UV exposure doesn't cause immediate flares. The effect is delayed.

The model tests different lag periods:
  • Same-day UV (no lag)
  • 24-hour lag (yesterday's UV affects today)
  • 48-hour lag (UV from 2 days ago)
  • 72-hour lag (UV from 3 days ago)

For each lag period, it:
  1. Pairs UV data with flare days
  2. Runs statistical tests (t-test, Cohen's d)
  3. Measures correlation strength
  4. Requires 30+ days of data for reliability

Currently, 24-hour lag shows the strongest correlation for this dataset.

Plain English: If you get too much sun today, you're more likely to feel it
tomorrow. The model learns your specific lag pattern from your own data.

USING THE LAB
─────────────
Commands:
  [1] weights   — View current symptom weights
  [2] adjust    — Adjust weights with sliders
  [3] simulate  — Run simulation to see how changes affect accuracy
  [4] code      — View the actual Python calculation code
  [6] achievements — See your tuning achievements
  [?] help      — Show this manual
  [X] exit      — Return to forecast page

Workflow:
  1. Adjust weights using sliders
  2. Run simulation to see impact on accuracy/recall/precision
  3. Review which predictions would flip
  4. Apply changes (currently manual — copy weights to app.py)

The goal is to balance:
  • Accuracy: Overall correctness
  • Recall: Catching actual flares (minimize false negatives)
  • Precision: Avoiding false alarms (minimize false positives)

APPLYING CHANGES
________________

## Step 1: Create the config directory

In your biotracking project root, create:
```
biotracking/
  ├── app.py
  ├── db.py
  ├── templates/
  ├── config/          ← CREATE THIS DIRECTORY
  │   └── .gitkeep     ← CREATE THIS EMPTY FILE (optional, keeps folder in git)
  └── ...
```

Run this from your project root:
```bash
mkdir -p config
touch config/.gitkeep
```

## Step 2: Update .gitignore

Add this line to your `.gitignore`:
```
config/custom_weights.json
```

This ensures your personal model tuning stays private.

## Step 4: Test the system

1. Restart Flask
2. Go to `/forecast/lab`
3. You should see "✓ Using factory defaults" at the top
4. Type `2` to adjust weights
5. Change a weight, run simulation
6. Click "✓ Apply These Changes"
7. Confirm the dialog
8. Page should reload showing "⚠ Custom weights active"
9. Check that `config/custom_weights.json` was created
10. Click "Reset to Defaults" to test reset functionality

## How it works:

**Before custom weights:**
- `calculate_flare_prime_score()` uses hardcoded DEFAULT_WEIGHTS
- No config file exists
- Lab shows "✓ Using factory defaults"

**After applying custom weights:**
- Lab saves to `config/custom_weights.json`
- `calculate_flare_prime_score()` loads from config via `get_current_weights()`
- All predictions use custom weights
- Lab shows "⚠ Custom weights active"

**After reset:**
- `config/custom_weights.json` is deleted
- Back to factory defaults
- Lab shows "✓ Using factory defaults"

## File contents example:

`config/custom_weights.json` after customization:
```json
{
  "neurological": 2.0,
  "cognitive": 1.25,
  "musculature": 1.75,
  "migraine": 1.0,
  "pulmonary": 1.0,
  "dermatological": 0.75,
  "mucosal": 0.25,
  "rheumatic": 0.5
}
```

## Troubleshooting:

**"Permission denied" error when applying:**
- Check that `config/` directory exists and is writable
- Run: `chmod 755 config/`

**Weights not taking effect:**
- Restart Flask after applying changes
- Check Flask console for error messages
- Verify `config/custom_weights.json` exists and is valid JSON

**Want to manually edit weights:**
- Edit `config/custom_weights.json` directly
- Restart Flask
- Changes will take effect immediately

## Safety notes:

- Custom weights are stored locally, never committed to git
- Original defaults are always preserved in code
- Reset button deletes custom config instantly
- Each user's biotracking instance has independent weights

REMOTE ACCESS (RASPBERRY PI + TAILSCALE)
─────────────────────────────────────────
If you want to access biotracking from your phone while away from home:

Setup Overview:
  Phone/Laptop (anywhere)
       ↓ (Tailscale encrypted tunnel)
  Oracle Cloud VM (public IP, exit node)
       ↓ (Tailscale encrypted tunnel)  
  Raspberry Pi (your home, running biotracking)
       ↓ (localhost)
  SQLite database (never leaves the Pi)

Why this works:
  • Starlink uses CGNAT — no static public IP, can't port forward
  • Tailscale creates encrypted mesh network between devices
  • Oracle VM provides stable public IP as exit node
  • Database stays on Pi, Oracle VM only sees encrypted traffic

Quick Setup:
  1. Install biotracking on Raspberry Pi (see README)
  2. Install Tailscale on Pi: curl -fsSL https://tailscale.com/install.sh | sh
  3. Create Oracle Cloud free tier VM
  4. Install Tailscale on VM
  5. Configure nginx reverse proxy on VM
  6. Open Oracle firewall (ports 80/443)
  7. Add HTTPS with Let's Encrypt (recommended)
  8. Add basic auth to nginx (required for security)

Full instructions: See REMOTE_ACCESS.md in the repository

Security Notes:
  ⚠ Always use HTTPS (Let's Encrypt is free)
  ⚠ Always use authentication (nginx basic auth minimum)
  ⚠ Keep software updated on Oracle VM
  ⚠ Review Tailscale ACLs to restrict access
  ⚠ Understand: anything on the internet has risk

The most secure setup is local-only. Remote access is a trade-off.
If you're in an unsafe situation, local-only may be the right choice.

MORE INFORMATION
────────────────
  • Full setup instructions: README.md
  • Contributing guide: CONTRIBUTING.md
  • Remote access details: REMOTE_ACCESS.md
  • Repository: github.com/alaricmoore/biotracking
  • Contact: alaric.moore@pm.me

This is a one-person project maintained between doctor appointments.
Response times may vary.

Take care of yourself out there.

────────────────────────────────────────────────────────────────────────────
Press any key to return to main menu
"""


# ============================================================
# Config loading
# ============================================================

def load_config() -> dict:
    """Load local config. Exits cleanly if setup hasn't been run."""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
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

from flask_wtf.csrf import CSRFProtect
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


@app.route('/favicon/<path:filename>')
def favicon_files(filename):
    """Serve favicon assets from images/favicon/."""
    return send_from_directory(os.path.join(app.root_path, 'images', 'favicon'), filename)


@app.before_request
def require_login():
    """Redirect unauthenticated users to login page."""
    if request.endpoint in ('login', 'register', 'static', 'favicon_files', 'api_health_sync', 'api_flare_status', 'api_uv_ingest', 'portal_view', 'portal_section', 'portal_document'):
        return
    if not current_user.is_authenticated:
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
# Medication reminder notifications (ntfy)
# ============================================================

def _send_ntfy(message: str) -> None:
    """Send a push notification via ntfy.sh (or self-hosted ntfy server)."""
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


def _check_and_send_reminders() -> None:
    """Background job: send ntfy notifications for doses due in the next minute."""
    now = datetime.now()
    window_end = now + timedelta(minutes=1)
    try:
        pending = db.get_all_pending_doses_with_ntfy(
            now.strftime("%Y-%m-%d %H:%M"),
            window_end.strftime("%Y-%m-%d %H:%M"),
        )
        for dose in pending:
            # Send to user's own ntfy topic
            topic = dose.get("ntfy_topic")
            server = dose.get("ntfy_server") or "https://ntfy.sh"
            if topic:
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

if not _is_reloader_parent:
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


# ============================================================
# Index
# ============================================================

@app.route("/")
def index():
    """Home page - redirects to daily entry for today."""
    return redirect(url_for("daily_entry"))


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
            next_page = request.args.get('next')
            return redirect(next_page or url_for("index"))
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
# Daily entry
# ============================================================

@app.route("/daily", methods=["GET"])
def daily_entry():
    """Daily entry form with date navigation. Defaults to today."""
    # Get date from query param or use today
    date_param = request.args.get("date")
    if date_param:
        try:
            entry_date = datetime.strptime(date_param, "%Y-%m-%d").date()
        except ValueError:
            entry_date = date.today()
    else:
        entry_date = date.today()
    
    entry_date_str = entry_date.isoformat()
    
    # Calculate prev/next dates
    prev_date = (entry_date - timedelta(days=1)).isoformat()
    next_date = (entry_date + timedelta(days=1)).isoformat()
    is_today = (entry_date == date.today())
    
    # Smart UV fetch — handles today's unresolved zeros gracefully
    prefs = db.get_user_preferences(uid()) or {}
    tz = prefs.get("timezone") or CONFIG.get("timezone", "America/Chicago")
    uv = uv_fetcher.smart_fetch_uv_for_date(entry_date_str, get_location_key(), tz)
    forecast = uv.get("forecast") if uv else None

    # Load any existing entry for this date
    existing = db.get_daily_observations(uid(), entry_date_str)
    
    # Load active medications for the sidebar
    active_meds = db.get_active_medications(uid())

    # Load today's scheduled doses for the reminder checklist
    todays_doses = db.get_todays_doses(uid(), entry_date_str)

    quick_mode = request.args.get("mode") == "quick"

    # BBT baseline hint for cycle trackers
    bbt_hint = None
    if prefs.get('track_cycle', CONFIG.get('track_cycle')):
        try:
            bbt_hint = _compute_bbt_hint(uid())
        except Exception:
            pass

    # Recent HealthKit sync events for the trust panel at the top of /daily
    try:
        recent_syncs = db.get_recent_health_sync_events(uid(), limit=3)
    except Exception:
        recent_syncs = []

    return render_template(
        "daily_entry.html",
        entry_date=entry_date_str,
        existing=existing,
        uv=uv,
        forecast=forecast,
        active_meds=active_meds,
        todays_doses=todays_doses,
        prev_date=prev_date,
        next_date=next_date,
        is_today=is_today,
        quick_mode=quick_mode,
        bbt_hint=bbt_hint,
        recent_syncs=recent_syncs,
    )


# The only valid flare severities. Anything else from a form is dropped to
# None before storage — the value later renders into the portal's burden
# chart, so keeping it to a known set is defense-in-depth against injection.
VALID_FLARE_SEVERITIES = ("minor", "major", "er_visit")


def _clean_flare_severity(raw):
    """Return raw only if it's a recognized severity, else None."""
    raw = (raw or "").strip()
    return raw if raw in VALID_FLARE_SEVERITIES else None


@app.route("/daily", methods=["POST"])
def daily_entry_submit():
    """Handle daily entry form submission."""
    form = request.form

    def get_bool(key):
        return 1 if form.get(key) == "on" else 0

    def get_float(key, default=None):
        val = form.get(key, "").strip()
        try:
            return float(val) if val else default
        except ValueError:
            return default

    data = {
        "date": form.get("date", date.today().isoformat()),
        "steps": get_float("steps"),
        "hours_slept": get_float("hours_slept"),
        "hrv": get_float("hrv"),
        "hrv_rmssd": get_float("hrv_rmssd"),
        "resting_heart_rate": get_float("resting_heart_rate"),
        "spo2": get_float("spo2"),
        "respiratory_rate": get_float("respiratory_rate"),
        "basal_temp_delta": get_float("basal_temp_delta"),
        "sun_exposure_min": get_float("sun_exposure_min"),
        "pain_scale": get_float("pain_scale"),
        "fatigue_scale": get_float("fatigue_scale"),
        "emotional_state": get_float("emotional_state"),
        "emotional_notes": form.get("emotional_notes", "").strip() or None,
        "neurological": get_bool("neurological"),
        "neuro_notes": form.get("neuro_notes", "").strip() or None,
        "cognitive": get_bool("cognitive"),
        "cognitive_notes": form.get("cognitive_notes", "").strip() or None,
        "musculature": get_bool("musculature"),
        "musculature_notes": form.get("musculature_notes", "").strip() or None,
        "migraine": get_bool("migraine"),
        "migraine_notes": form.get("migraine_notes", "").strip() or None,
        "pulmonary": get_bool("pulmonary"),
        "pulmonary_notes": form.get("pulmonary_notes", "").strip() or None,
        "gastro": get_bool("gastro"),
        "gastro_notes": form.get("gastro_notes", "").strip() or None,
        "mucosal": get_bool("mucosal"),
        "mucosal_notes": form.get("mucosal_notes", "").strip() or None,
        "dermatological": get_bool("dermatological"),
        "derm_notes": form.get("derm_notes", "").strip() or None,
        "rheumatic": get_bool("rheumatic"),
        "rheumatic_notes": form.get("rheumatic_notes", "").strip() or None,
        "strike_physical": get_bool("strike_physical"),
        "strike_environmental": get_bool("strike_environmental"),
        "flare_occurred": get_bool("flare_occurred"),
        "flare_severity": _clean_flare_severity(form.get("flare_severity")) if form.get("flare_occurred") else None,
        "notes": form.get("notes", "").strip() or None,
        "period_flow": form.get("period_flow") or None,
        "cramping": form.get("cramping") or None,
        "cycle_notes": form.get("cycle_notes", "").strip() or None,
        "stayed_indoors": 1 if form.get("stayed_indoors") else 0,
        "uv_protection_level": form.get("uv_protection_level") or None,
    }

    # If stayed indoors, force consistent values
    if data["stayed_indoors"]:
        data["sun_exposure_min"] = 0
        data["uv_protection_level"] = "indoors_only"

    db.upsert_daily_observations(uid(), data)
    db.upsert_user_preferences(uid(), {"last_logged_at": datetime.now().isoformat()})
    return redirect(url_for("daily_confirm", entry_date=data["date"]))


@app.route("/uv/manual", methods=["POST"])
@login_required
def uv_manual():
    """Save manually entered UV values."""
    data = request.get_json(force=True)
    date_str = data.get("date", date.today().isoformat())
    uv_fetcher.store_manual_uv(
        date_str=date_str,
        uv_morning=float(data.get("uv_morning", 0)),
        uv_noon=float(data.get("uv_noon", 0)),
        uv_evening=float(data.get("uv_evening", 0)),
        location_key=get_location_key(),
    )
    return jsonify({"ok": True})


@app.route("/daily/confirm/<entry_date>")
def daily_confirm(entry_date):
    """Confirmation screen after daily entry submission."""
    entry = db.get_daily_observations(uid(), entry_date)
    uv = db.get_uv_data(get_location_key(), entry_date)
    return render_template("daily_confirm.html", entry=entry, uv=uv)


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
# Clinical record
# ============================================================

@app.route("/clinical")
def clinical_record():
    """Record - labs, ANA, meds, events, clinicians."""
    labs = db.get_lab_results(uid())
    ana = db.get_ana_results(uid())
    meds = db.get_all_medications(uid())
    events = db.get_clinical_events(uid())
    clinicians = db.get_all_clinicians(uid())
    documents = db.get_clinical_documents(uid())
    test_names = db.get_lab_test_names(uid())

    # Split active/inactive meds
    today_str = date.today().isoformat()
    active = [m for m in meds
              if m["start_date"] <= today_str and
                 (m.get("end_date") is None or m["end_date"] >= today_str)]
    inactive = [m for m in meds
                if m.get("end_date") and m["end_date"] < today_str]

    # Build taper schedule lookup keyed by medication_id
    taper_by_med = {}
    for med in active:
        t = db.get_active_taper_for_medication(uid(), med["id"])
        if t:
            taper_by_med[med["id"]] = t

    prefs = get_user_prefs()
    ntfy_configured = bool(prefs.get("ntfy_topic") or CONFIG.get("ntfy_topic"))

    # Flare history for backfill tab
    all_obs = db.get_all_daily_observations(uid())
    flare_history = [
        {"date": o["date"], "flare_severity": o.get("flare_severity"), "notes": o.get("notes")}
        for o in sorted(all_obs, key=lambda x: x["date"], reverse=True)
        if o.get("flare_occurred") == 1
    ]

    return render_template(
        "clinical_record.html",
        labs=labs,
        ana=ana,
        meds=meds,
        active=active,
        inactive=inactive,
        events=events,
        clinicians=clinicians,
        documents=documents,
        test_names=test_names,
        today=date.today().isoformat(),
        taper_by_med=taper_by_med,
        ntfy_configured=ntfy_configured,
        flare_history=flare_history,
    )
    
@app.route("/medication/update/<int:med_id>", methods=["POST"])
def update_medication(med_id):
    """Update an existing medication. Auto-logs a dose_change event when the
    dose value or its unit meaningfully changes; first-time dose entry is
    suppressed so brand-new doses don't show up as a "change"."""
    form = request.form

    current = db.get_medication(uid(), med_id)
    old_dose = current["dose"] if current else None
    old_unit = current["unit"] if current else None

    new_dose = float(form.get("dose")) if form.get("dose") else None
    new_unit = form.get("unit") or None

    db.update_medication(
        user_id=uid(),
        med_id=med_id,
        drug_name=form.get("drug_name"),
        dose=new_dose,
        unit=new_unit,
        frequency=form.get("frequency") or None,
        category=form.get("category") or None,
        indication=form.get("indication") or None,
        start_date=form.get("start_date"),
        end_date=form.get("end_date") or None,
        notes=form.get("notes") or None,
        is_primary_intervention=form.get("is_primary_intervention") == "1",
        is_secondary_intervention=form.get("is_secondary_intervention") == "1",
    )

    should_log = False
    if old_dose is not None and new_dose is not None:
        if old_dose != new_dose or old_unit != new_unit:
            should_log = True
    elif old_dose is not None and new_dose is None:
        should_log = True

    if should_log:
        if old_unit == new_unit and old_unit and new_dose is not None:
            note = f"dose: {old_dose} → {new_dose} {old_unit}"
        else:
            old_str = f"{old_dose} {old_unit}" if old_unit else f"{old_dose}"
            new_str = "(removed)" if new_dose is None else (
                f"{new_dose} {new_unit}" if new_unit else f"{new_dose}"
            )
            note = f"dose: {old_str} → {new_str}"
        db.add_medication_event(
            user_id=uid(),
            medication_id=med_id,
            event_date=date.today().isoformat(),
            event_type="dose_change",
            severity=None,
            note=note,
        )

    return redirect(url_for("clinical_record") + "#medications")


@app.route("/medication/delete/<int:med_id>", methods=["POST"])
def delete_medication(med_id):
    """Delete a medication."""
    db.delete_medication(uid(), med_id)
    return redirect(url_for("clinical_record") + "#medications")


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


#============================================================
# Clinician management
#============================================================

@app.route("/clinician/add", methods=["POST"])
def add_clinician():
    """Add a new clinician."""
    db.add_clinician(uid(), {
        "name": request.form.get("name"),
        "specialty": request.form.get("specialty"),
        "clinic_name": request.form.get("clinic_name") or None,
        "address": request.form.get("address") or None,
        "phone": request.form.get("phone") or None,
        "email": request.form.get("email") or None,
        "network": request.form.get("network") or None,
        "notes": request.form.get("notes") or None,
    })
    return redirect(url_for("clinical_record") + "#clinicians")


@app.route("/clinician/update/<int:clinician_id>", methods=["POST"])
def update_clinician(clinician_id):
    """Update an existing clinician."""
    form = request.form
    
    db.update_clinician(
        user_id=uid(),
        clinician_id=clinician_id,
        name=form.get("name"),
        specialty=form.get("specialty"),
        clinic_name=form.get("clinic_name") or None,
        address=form.get("address") or None,
        phone=form.get("phone") or None,
        email=form.get("email") or None,
        network=form.get("network") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#clinicians")


@app.route("/clinician/delete/<int:clinician_id>", methods=["POST"])
def delete_clinician(clinician_id):
    """Delete a clinician."""
    db.delete_clinician(uid(), clinician_id)
    return redirect(url_for("clinical_record") + "#clinicians")


# ============================================================
# Clinical record - add entries
# ============================================================

@app.route("/clinical/lab/add", methods=["POST"])
def add_lab():
    """Add a lab result."""
    form = request.form
    data = {
        "date": form.get("date"),
        "test_name": form.get("test_name", "").strip(),
        "numeric_value": float(form["numeric_value"])
            if form.get("numeric_value", "").strip() else None,
        "unit": form.get("unit", "").strip() or None,
        "qualitative_result": form.get("qualitative_result", "").strip() or None,
        "reference_range": form.get("reference_range", "").strip() or None,
        "flag": form.get("flag", "").strip() or None,
        "provider": form.get("provider", "").strip() or None,
        "lab_facility": form.get("lab_facility", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
    }
    db.add_lab_result(uid(), data)
    return redirect(url_for("clinical_record") + "#labs")


# ============================================================
# Bulk lab import (CSV upload / paste) with a review step
# ============================================================

def _lab_dedup_key(date_str, test_name, num, qual):
    """Stable key for spotting a lab already in the record.

    Numeric values are formatted with %g so "3" and "3.0" collapse to one key.
    """
    if num is not None:
        try:
            v = "%g" % float(num)
        except (TypeError, ValueError):
            v = str(num).strip().lower()
    else:
        v = (qual or "").strip().lower()
    return (date_str, (test_name or "").strip().lower(), v)


def _normalize_lab_rows(text, existing_keys):
    """Parse a lab CSV (Date, Test, Value, Units[, Lab, Doctor, ...]) into
    lab dicts, auto-filling reference range/flag for known tests and tagging
    each row 'new' or 'duplicate' against what's already stored.

    Header matching is case-insensitive and tolerant of a few aliases, so the
    same parser handles the portal exports and the hand-kept CSVs.
    """
    import csv, io
    from import_labs import lookup_reference, parse_date, parse_float

    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        low = {(k or "").strip().lower(): (v or "").strip()
               for k, v in raw.items()}

        def col(*names):
            for n in names:
                if low.get(n):
                    return low[n]
            return ""

        date_str = parse_date(col("date", "collected", "date collected", "result date"))
        test = col("test", "test_name", "test name", "analyte", "name")
        valraw = col("value", "result", "numeric_value", "observation value")
        if not date_str or not test or not valraw:
            continue

        # Keep bounded/inequality results ("<20", ">24.0") and titers as text.
        # parse_float would strip the "<" and store a bare 20, which reads like a
        # real value at the threshold rather than a negative below it.
        if "<" in valraw or ">" in valraw:
            num, qual = None, valraw
        else:
            num = parse_float(valraw)
            qual = None if num is not None else valraw
        unit = col("units", "unit") or None
        provider = col("doctor", "provider", "ordering provider") or None
        facility = col("lab", "facility", "lab_facility", "lab facility") or None
        refrange = col("reference", "reference_range", "reference range", "range") or None
        flag = col("flag", "abnormal") or None

        if num is not None and (not refrange or not flag):
            rr, fl = lookup_reference(test, num)
            refrange = refrange or rr
            flag = flag or fl

        key = _lab_dedup_key(date_str, test, num, qual)
        rows.append({
            "date": date_str, "test_name": test,
            "numeric_value": num, "qualitative_result": qual,
            "unit": unit, "reference_range": refrange, "flag": flag,
            "provider": provider, "lab_facility": facility,
            "status": "duplicate" if key in existing_keys else "new",
        })
    return rows


@app.route("/clinical/labs/import/preview", methods=["POST"])
@login_required
def import_labs_preview():
    """Parse an uploaded or pasted lab CSV and show a review table.

    Nothing is written yet — the parsed rows ride along as hidden JSON and are
    only committed from the preview page, so a bad file never touches the DB.
    """
    text = ""
    f = request.files.get("csv_file")
    if f and f.filename:
        text = f.read().decode("utf-8", errors="replace")
    if not text.strip():
        text = request.form.get("csv_text", "") or ""
    if not text.strip():
        return redirect(url_for("clinical_record") + "#labs")

    existing = db.get_lab_results(uid())
    existing_keys = {
        _lab_dedup_key(l["date"], l["test_name"],
                       l.get("numeric_value"), l.get("qualitative_result"))
        for l in existing
    }
    rows = _normalize_lab_rows(text, existing_keys)
    return render_template(
        "lab_import_preview.html",
        rows=rows,
        rows_json=json.dumps(rows),
        n_new=sum(1 for r in rows if r["status"] == "new"),
        n_dup=sum(1 for r in rows if r["status"] == "duplicate"),
    )


@app.route("/clinical/labs/import/commit", methods=["POST"])
@login_required
def import_labs_commit():
    """Insert only the rows the user kept checked on the preview page."""
    try:
        rows = json.loads(request.form.get("rows_json", "[]"))
    except (ValueError, TypeError):
        rows = []
    include = set(request.form.getlist("include"))
    inserted = 0
    for i, r in enumerate(rows):
        if str(i) not in include:
            continue
        data = {k: r.get(k) for k in (
            "date", "test_name", "numeric_value", "qualitative_result",
            "unit", "reference_range", "flag", "provider", "lab_facility")}
        try:
            db.add_lab_result(uid(), data)
            inserted += 1
        except Exception:
            continue
    return redirect(url_for("clinical_record") + "#labs")


# ============================================================
# Clinical document library (uploaded PDFs, stored on disk)
# ============================================================
DOCUMENTS_DIR = os.path.join(os.path.dirname(__file__), "documents")


def _user_docs_dir(user_id: int) -> str:
    d = os.path.join(DOCUMENTS_DIR, f"user_{user_id}")
    os.makedirs(d, exist_ok=True)
    return d


def _extract_pdf_text(blob: bytes) -> str | None:
    """Best-effort text extraction for search. Prefers poppler's pdftotext
    (usually present, much better results here), falls back to pypdf. Returns
    None for scanned PDFs (no text layer) or if neither is available — the
    summary field carries search then, so this never blocks an upload."""
    # 1) pdftotext (poppler)
    try:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
            tf.write(blob)
            tf.flush()
            out = subprocess.run(["pdftotext", "-q", tf.name, "-"],
                                 capture_output=True, timeout=30)
        text = out.stdout.decode("utf-8", "replace").strip()
        if text:
            return text[:100000]
    except Exception:
        pass
    # 2) pypdf fallback
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(blob))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        return text[:100000] or None
    except Exception:
        return None


@app.route("/clinical/document/add", methods=["POST"])
@login_required
def add_document():
    """Store an uploaded PDF on disk and record its metadata.

    The file is saved under a random token name (no traversal, no collisions);
    the original filename is kept only for display and download.
    """
    import secrets
    f = request.files.get("pdf_file")
    form = request.form
    if not (f and f.filename) or not f.filename.lower().endswith(".pdf"):
        return redirect(url_for("clinical_record") + "#documents")
    blob = f.read()
    if not blob or len(blob) > 20 * 1024 * 1024:   # 20 MB cap
        return redirect(url_for("clinical_record") + "#documents")

    title = (form.get("title") or "").strip() or \
        os.path.splitext(os.path.basename(f.filename))[0]
    stored = secrets.token_hex(16) + ".pdf"
    with open(os.path.join(_user_docs_dir(uid()), stored), "wb") as out:
        out.write(blob)
    db.add_clinical_document(uid(), {
        "date": form.get("date") or None,
        "title": title,
        "doc_type": form.get("doc_type") or None,
        "specialty": form.get("specialty") or None,
        "provider": form.get("provider") or None,
        "facility": form.get("facility") or None,
        "file_name": stored,
        "orig_name": os.path.basename(f.filename),
        "summary": (form.get("summary") or "").strip() or None,
        "extracted_text": _extract_pdf_text(blob),
    })
    return redirect(url_for("clinical_record") + "#documents")


@app.route("/clinical/document/<int:doc_id>/file")
@login_required
def document_file(doc_id):
    """Serve a stored PDF, scoped to the owning user. Inline by default;
    ?download=1 forces a download."""
    doc = db.get_clinical_document(uid(), doc_id)
    if not doc or not doc.get("file_name"):
        return Response("Not found", status=404)
    return send_from_directory(
        _user_docs_dir(uid()), doc["file_name"],
        mimetype="application/pdf",
        as_attachment=bool(request.args.get("download")),
        download_name=doc.get("orig_name") or "document.pdf",
    )


@app.route("/clinical/document/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    """Remove a document row and its file from disk (scoped to the user)."""
    fname = db.delete_clinical_document(uid(), doc_id)
    if fname:
        try:
            os.remove(os.path.join(_user_docs_dir(uid()), fname))
        except OSError:
            pass
    return redirect(url_for("clinical_record") + "#documents")


# ============================================================
# Clinician portal — capability-link, READ-ONLY, per-specialty
# ============================================================
# Public routes here take a token, not a login. They MUST NOT reach any write,
# admin, or export endpoint — this section only ever reads curated data for the
# link's own user. Management routes below (/portals*) require login as normal.

PORTAL_VIEWS = {"full": "Full record"}   # one read-only record, not per-specialty


def _valid_portal_link(token: str):
    """Return the link row if the token is real, not revoked, not expired —
    else None. This is the sole gate for portal access."""
    link = db.get_portal_link_by_token(token)
    if not link or link.get("revoked_at"):
        return None
    exp = link.get("expires_at")
    if exp and exp < datetime.utcnow().isoformat():
        return None
    return link


@app.after_request
def _portal_security_headers(resp):
    """Keep portal pages out of indexes and shared caches."""
    if request.path.startswith("/portal/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _portal_identity(prefs: dict) -> dict:
    dob = prefs.get("patient_dob")
    age = None
    if dob:
        try:
            b = datetime.strptime(dob, "%Y-%m-%d").date()
            t = date.today()
            age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
        except ValueError:
            pass
    return {"name": prefs.get("patient_name"), "dob": dob, "age": age}


def _owner_location_key(prefs: dict) -> str:
    lat = prefs.get("location_lat") or CONFIG.get("location_lat")
    lon = prefs.get("location_lon") or CONFIG.get("location_lon")
    if lat and lon:
        return db.make_location_key(float(lat), float(lon))
    return "default"


_IMMUNOSUPPRESSANTS = ("hydroxychloroquine", "plaquenil", "mycophenolate",
                       "cellcept", "methotrexate", "azathioprine", "rituximab",
                       "belimumab", "prednisone", "methylprednisolone")


def _portal_common_ctx(link):
    """Shared template context for every portal page, plus the owner id and
    preferences the per-page data builders need."""
    owner_id = link["user_id"]
    clinician = None
    if link.get("clinician_id"):
        clinician = next((c for c in db.get_all_clinicians(owner_id)
                          if c["id"] == link["clinician_id"]), None)
    prefs = db.get_user_preferences(owner_id) or {}
    ctx = dict(link=link, clinician=clinician, view_label="Clinical record",
               patient=_portal_identity(prefs))
    return ctx, owner_id, prefs


def _portal_meds(owner_id: int):
    """(active, past) medications for an owner — active immuno-flagged and
    sorted immuno first, past sorted by end date, newest first."""
    today = date.today().isoformat()
    meds = db.get_all_medications(owner_id)
    active = [m for m in meds if (m.get("start_date") or "") <= today
              and (not m.get("end_date") or m["end_date"] >= today)]
    for m in active:
        m["_immuno"] = any(k in (m.get("drug_name") or "").lower()
                           for k in _IMMUNOSUPPRESSANTS)
    active.sort(key=lambda m: (not m["_immuno"], m.get("drug_name") or ""))
    inactive = sorted([m for m in meds if m.get("end_date") and m["end_date"] < today],
                      key=lambda m: m.get("end_date") or "", reverse=True)
    return active, inactive


def _portal_labs(owner_id: int) -> dict:
    all_labs = db.get_lab_results(owner_id)
    return {
        "key_labs": select_report_labs(all_labs),
        "all_labs": sorted(all_labs, key=lambda x: x.get("date") or "", reverse=True),
        "ana_history": sorted(db.get_ana_results(owner_id),
                              key=lambda x: x.get("date") or "", reverse=True),
    }


def _portal_symptom_window(owner_id: int) -> dict:
    """Last-90-day patient-log summary: period stats, symptom frequency, and
    the flares themselves."""
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=90)).isoformat()
    observations = [o for o in db.get_all_daily_observations(owner_id)
                    if start <= o["date"] <= end]
    pain = [o["pain_scale"] for o in observations if o.get("pain_scale") is not None]
    fatigue = [o["fatigue_scale"] for o in observations
               if o.get("fatigue_scale") is not None]
    flares = sorted(
        [{"date": o["date"], "severity": o.get("flare_severity"),
          "pain": o.get("pain_scale"), "fatigue": o.get("fatigue_scale")}
         for o in observations if o.get("flare_occurred")],
        key=lambda f: f["date"], reverse=True)
    return {
        "observations": observations,
        "period": {"start": start, "end": end, "obs_days": len(observations),
                   "flare_days": len(flares),
                   "mean_pain": round(sum(pain) / len(pain), 1) if pain else None,
                   "mean_fatigue": round(sum(fatigue) / len(fatigue), 1) if fatigue else None},
        "symptom_freq": symptom_frequency(observations),
        "flares": flares,
    }


# The record's sections: url slug -> (card title, context builder). The
# overview page links a card to each; /portal/<token>/<section> serves them.
PORTAL_SECTIONS = {
    "documents":   ("Documents",
                    lambda owner_id, prefs: {"documents": db.get_clinical_documents(owner_id)}),
    "medications": ("Medications",
                    lambda owner_id, prefs: dict(zip(("active_meds", "inactive_meds"),
                                                     _portal_meds(owner_id)))),
    "labs":        ("Lab results",
                    lambda owner_id, prefs: _portal_labs(owner_id)),
    "timeline":    ("Clinical timeline",
                    lambda owner_id, prefs: {"events": sorted(db.get_clinical_events(owner_id),
                                                              key=lambda x: x.get("date") or "",
                                                              reverse=True)}),
    "symptoms":    ("Symptom history",
                    lambda owner_id, prefs: _portal_symptom_window(owner_id)),
}


@app.route("/portal/<token>")
def portal_view(token):
    """The clinician-facing landing page: synopsis, disease-burden chart, and
    auto-findings up top, then a card per record section. No login: the token
    is the key."""
    link = _valid_portal_link(token)
    if not link:
        return Response(render_template("portal_invalid.html"), status=403)
    db.record_portal_access(link["id"], request.path)
    ctx, owner_id, prefs = _portal_common_ctx(link)

    sym = _portal_symptom_window(owner_id)
    period = sym["period"]
    loc_key = _owner_location_key(prefs)
    uv = (db.get_uv_data_range(loc_key, period["start"], period["end"])
          if sym["observations"] else [])
    labs = _portal_labs(owner_id)
    active_meds, inactive_meds = _portal_meds(owner_id)
    events = db.get_clinical_events(owner_id)
    documents = db.get_clinical_documents(owner_id)

    all_obs = sorted(db.get_all_daily_observations(owner_id), key=lambda x: x["date"])
    burden = _burden_series(all_obs, period["start"], period["end"], loc_key, owner_id)

    period_days = max((date.fromisoformat(period["end"])
                       - date.fromisoformat(period["start"])).days, 1)
    ctx.update(
        period=period,
        synopsis={
            "flares_per_month": round(period["flare_days"] / period_days * 30, 1),
            "flare_count": period["flare_days"],
            "period_days": period_days,
            "dmards": [m["drug_name"] for m in active_meds if m["_immuno"]],
            "serology": _serology_tags(labs["key_labs"]),
            "mean_pain": period["mean_pain"],
            "mean_fatigue": period["mean_fatigue"],
        },
        findings=generate_findings(sym["observations"], uv, period["start"],
                                   period["end"], user_id=owner_id),
        burden=burden,
        burden_threshold=get_current_weights(owner_id).get("flare_threshold", 8.0),
        counts={"documents": len(documents), "labs": len(labs["all_labs"]),
                "ana": len(labs["ana_history"]), "meds_active": len(active_meds),
                "meds_past": len(inactive_meds), "events": len(events),
                "obs_days": period["obs_days"]},
    )
    return render_template("portal_overview.html", **ctx)


@app.route("/portal/<token>/<section>")
def portal_section(token, section):
    """One section of the read-only record (documents, medications, labs,
    timeline, symptoms) — reached from the overview's cards."""
    link = _valid_portal_link(token)
    if not link:
        return Response(render_template("portal_invalid.html"), status=403)
    if section not in PORTAL_SECTIONS:
        return Response("Not found", status=404)
    db.record_portal_access(link["id"], request.path)
    ctx, owner_id, prefs = _portal_common_ctx(link)
    title, build = PORTAL_SECTIONS[section]
    ctx["section_title"] = title
    ctx.update(build(owner_id, prefs))
    return render_template(f"portal_{section}.html", **ctx)


@app.route("/portal/<token>/document/<int:doc_id>")
def portal_document(token, doc_id):
    """Serve one of the owner's PDFs through a valid portal link — read-only,
    scoped to the link's user, so a token can only reach that patient's docs."""
    link = _valid_portal_link(token)
    if not link:
        return Response("Not found", status=404)
    doc = db.get_clinical_document(link["user_id"], doc_id)
    if not doc or not doc.get("file_name"):
        return Response("Not found", status=404)
    db.record_portal_access(link["id"], request.path)
    return send_from_directory(
        _user_docs_dir(link["user_id"]), doc["file_name"],
        mimetype="application/pdf", as_attachment=False,
        download_name=doc.get("orig_name") or "document.pdf")


@app.route("/portals")
@login_required
def portals_manage():
    links = db.get_portal_links(uid())
    return render_template(
        "portal_manage.html",
        links=links,
        clinicians=db.get_all_clinicians(uid()),
        access_log=db.get_portal_access_log(uid(), limit=50),
        views=PORTAL_VIEWS,
        now=datetime.utcnow().isoformat(),
        base_url=request.host_url.rstrip("/"),
    )


@app.route("/portals/create", methods=["POST"])
@login_required
def portals_create():
    form = request.form
    clinician_id = int(form["clinician_id"]) if form.get("clinician_id") else None
    label = (form.get("label") or "").strip() or None
    try:
        days = max(1, min(365, int(form.get("days") or 30)))
    except ValueError:
        days = 30
    expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
    db.create_portal_link(uid(), clinician_id, "full", label, expires_at)
    return redirect(url_for("portals_manage"))


@app.route("/portals/<int:link_id>/revoke", methods=["POST"])
@login_required
def portals_revoke(link_id):
    db.revoke_portal_link(uid(), link_id)
    return redirect(url_for("portals_manage"))


@app.route("/clinical/ana/add", methods=["POST"])
def add_ana():
    """Add an ANA result."""
    form = request.form
    patterns_raw = form.get("patterns", "").strip()
    patterns = [p.strip() for p in patterns_raw.split(",") if p.strip()]

    db.add_ana_result(
        user_id=uid(),
        date_str=form.get("date"),
        titer_integer=int(form["titer"]) if form.get("titer", "").strip() else None,
        screen_result=form.get("screen_result", "").strip(),
        patterns=patterns,
        provider=form.get("provider", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
    )
    return redirect(url_for("clinical_record") + "#ana")


@app.route("/clinical/event/add", methods=["POST"])
def add_event():
    """Add a clinical event."""
    form = request.form
    data = {
        "date": form.get("date"),
        "event_type": form.get("event_type", "").strip(),
        "provider": form.get("provider", "").strip() or None,
        "facility": form.get("facility", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "follow_up_date": form.get("follow_up_date", "").strip() or None,
    }
    db.add_clinical_event(uid(), data)
    return redirect(url_for("clinical_record") + "#events")


@app.route("/backfill/flare", methods=["POST"])
@login_required
def backfill_flare():
    """Record a past flare event (backfill for gaps in tracking)."""
    form = request.form
    flare_date = form.get("date", "").strip()
    severity = form.get("flare_severity", "").strip()
    notes = form.get("notes", "").strip() or None

    if not flare_date or severity not in VALID_FLARE_SEVERITIES:
        return redirect(url_for("clinical_record", msg="Date and severity are required.") + "#backfill")

    try:
        parsed_date = date.fromisoformat(flare_date)
    except ValueError:
        return redirect(url_for("clinical_record", msg="Invalid date format.") + "#backfill")

    if parsed_date > date.today():
        return redirect(url_for("clinical_record", msg="Cannot backfill a future date.") + "#backfill")

    data = {
        "date": flare_date,
        "flare_occurred": 1,
        "flare_severity": severity,
    }
    if notes:
        data["notes"] = notes

    db.upsert_daily_observations(uid(), data)
    severity_label = "ER visit" if severity == "er_visit" else severity
    return redirect(url_for("clinical_record", msg=f"Recorded {severity_label} flare on {flare_date}.") + "#backfill")


@app.route("/backfill/flare/update", methods=["POST"])
@login_required
def backfill_flare_update():
    """Update a backfilled flare entry."""
    form = request.form
    flare_date = form.get("date", "").strip()
    severity = form.get("flare_severity", "").strip()
    notes = form.get("notes", "").strip() or None

    if not flare_date or severity not in VALID_FLARE_SEVERITIES:
        return redirect(url_for("clinical_record", msg="Date and severity are required.") + "#backfill")

    data = {
        "date": flare_date,
        "flare_occurred": 1,
        "flare_severity": severity,
    }
    if notes is not None:
        data["notes"] = notes

    db.upsert_daily_observations(uid(), data)
    return redirect(url_for("clinical_record", msg=f"Updated flare on {flare_date}.") + "#backfill")


@app.route("/backfill/flare/delete", methods=["POST"])
@login_required
def backfill_flare_delete():
    """Remove flare flag from a daily observation (doesn't delete the whole row)."""
    flare_date = request.form.get("date", "").strip()
    if flare_date:
        data = {
            "date": flare_date,
            "flare_occurred": 0,
            "flare_severity": None,
        }
        db.upsert_daily_observations(uid(), data)
    return redirect(url_for("clinical_record", msg=f"Removed flare on {flare_date}.") + "#backfill")


@app.route("/medication/add", methods=["POST"])
def add_medication():
    """Add a new medication."""
    db.add_medication(uid(), {
        "drug_name": request.form.get("drug_name"),
        "dose": request.form.get("dose"),
        "unit": request.form.get("unit"),
        "frequency": request.form.get("frequency"),
        "route": request.form.get("route"),
        "category": request.form.get("category"),
        "indication": request.form.get("indication"),
        "start_date": request.form.get("start_date"),
        "end_date": request.form.get("end_date") or None,
        "notes": request.form.get("notes"),
        "is_primary_intervention": request.form.get("is_primary_intervention") == "1",
        "is_secondary_intervention": request.form.get("is_secondary_intervention") == "1",
    })
    return redirect(url_for("clinical_record") + "#medications")

#=======================================
# Edit/Cancel/Delete
#=======================================

@app.route("/clinical/medication/end/<int:med_id>", methods=["POST"])
def end_medication(med_id):
    """Mark a medication as ended today."""
    end_date = request.form.get("end_date", date.today().isoformat())
    db.end_medication(uid(), med_id, end_date)
    return redirect(url_for("clinical_record") + "#medications")

# lab results update/delete

@app.route("/lab/update/<int:lab_id>", methods=["POST"])
def update_lab(lab_id):
    """Update an existing lab result."""
    form = request.form
    
    def get_float(key):
        val = form.get(key, "").strip()
        try:
            return float(val) if val else None
        except ValueError:
            return None
    
    db.update_lab_result(
        user_id=uid(),
        lab_id=lab_id,
        date=form.get("date"),
        test_name=form.get("test_name"),
        numeric_value=get_float("numeric_value"),
        unit=form.get("unit") or None,
        qualitative_result=form.get("qualitative_result") or None,
        reference_range=form.get("reference_range") or None,
        flag=form.get("flag") or None,
        provider=form.get("provider") or None,
        lab_facility=form.get("lab_facility") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#labs")


@app.route("/lab/delete/<int:lab_id>", methods=["POST"])
def delete_lab(lab_id):
    """Delete a lab result."""
    db.delete_lab_result(uid(), lab_id)
    return redirect(url_for("clinical_record") + "#labs")


@app.route("/ana/update/<int:ana_id>", methods=["POST"])
def update_ana(ana_id):
    """Update an existing ANA result."""
    form = request.form
    
    db.update_ana_result(
        user_id=uid(),
        ana_id=ana_id,
        date=form.get("date"),
        titer=form.get("titer") or None,
        patterns=form.get("patterns") or None,
        screen_result=form.get("screen_result") or None,
        provider=form.get("provider") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#ana")


@app.route("/ana/delete/<int:ana_id>", methods=["POST"])
def delete_ana(ana_id):
    """Delete an ANA result."""
    db.delete_ana_result(uid(), ana_id)
    return redirect(url_for("clinical_record") + "#ana")


@app.route("/event/update/<int:event_id>", methods=["POST"])
def update_event(event_id):
    """Update an existing clinical event."""
    form = request.form
    
    db.update_clinical_event(
        user_id=uid(),
        event_id=event_id,
        date=form.get("date"),
        event_type=form.get("event_type"),
        provider=form.get("provider") or None,
        facility=form.get("facility") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#events")


@app.route("/event/delete/<int:event_id>", methods=["POST"])
def delete_event(event_id):
    """Delete a clinical event."""
    db.delete_clinical_event(uid(), event_id)
    return redirect(url_for("clinical_record") + "#events")

#======================================
# Export Lab/Meds/Clinicians/Events
#======================================

import csv
from io import StringIO
from flask import Response


def _write_patient_header(writer):
    """Write patient name/DOB metadata rows at the top of a CSV export."""
    prefs = get_user_prefs()
    name = prefs.get("patient_name") or CONFIG.get("patient_name", "")
    dob = prefs.get("patient_dob") or CONFIG.get("patient_dob", "")
    writer.writerow(["Patient:", name, "DOB:", dob])
    writer.writerow(["Export date:", date.today().isoformat()])
    writer.writerow([])

@app.route("/export/labs")
def export_labs():
    """Export lab results as CSV within date range."""
    start_date = request.args.get("start")
    end_date = request.args.get("end")
    
    if not start_date or not end_date:
        return "Missing date range parameters", 400
    
    # Get labs in date range
    all_labs = db.get_lab_results(uid())
    filtered_labs = [
        lab for lab in all_labs
        if start_date <= lab["date"] <= end_date
    ]
    
    # Sort by date (most recent first)
    filtered_labs.sort(key=lambda x: x["date"], reverse=True)
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Date',
        'Test Name',
        'Numeric Value',
        'Unit',
        'Qualitative Result',
        'Reference Range',
        'Flag',
        'Provider',
        'Lab Facility',
        'Notes'
    ])
    
    # Write data rows
    for lab in filtered_labs:
        writer.writerow([
            lab.get('date', ''),
            lab.get('test_name', ''),
            lab.get('numeric_value', '') if lab.get('numeric_value') is not None else '',
            lab.get('unit', '') or '',
            lab.get('qualitative_result', '') or '',
            lab.get('reference_range', '') or '',
            lab.get('flag', '') or '',
            lab.get('provider', '') or '',
            lab.get('lab_facility', '') or '',
            lab.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename with date range
    filename = f"lab_results_{start_date}_to_{end_date}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route("/export/clinicians")
def export_clinicians():
    """Export all clinicians as CSV."""
    
    # Get all clinicians
    clinicians = db.get_all_clinicians(uid())
    
    # Sort by name
    clinicians.sort(key=lambda x: x.get('name', '').lower())
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Name',
        'Specialty',
        'Clinic Name',
        'Phone',
        'Email/Portal',
        'Network',
        'Address',
        'Notes'
    ])
    
    # Write data rows
    for c in clinicians:
        writer.writerow([
            c.get('name', ''),
            c.get('specialty', ''),
            c.get('clinic_name', '') or '',
            c.get('phone', '') or '',
            c.get('email', '') or '',
            c.get('network', '') or '',
            c.get('address', '') or '',
            c.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename with today's date
    today = date.today().isoformat()
    filename = f"clinicians_{today}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
    
@app.route("/export/medications")
def export_medications():
    """Export medications as CSV with filter (active/all/inactive)."""
    
    filter_type = request.args.get("filter", "active")
    
    # Get all medications
    all_meds = db.get_all_medications(uid())
    
    # Filter based on selection
    today_str = date.today().isoformat()
    
    if filter_type == "active":
        filtered_meds = [
            m for m in all_meds 
            if m["start_date"] <= today_str and
               (m.get("end_date") is None or m["end_date"] >= today_str)
        ]
        filename_suffix = "active"
    elif filter_type == "inactive":
        filtered_meds = [
            m for m in all_meds 
            if m.get("end_date") and m["end_date"] < today_str
        ]
        filename_suffix = "inactive"
    else:  # all
        filtered_meds = all_meds
        filename_suffix = "all"
    
    # Sort by start date (most recent first)
    filtered_meds.sort(key=lambda x: x.get("start_date", ""), reverse=True)
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Drug Name',
        'Dose',
        'Unit',
        'Frequency',
        'Route',
        'Category',
        'Indication',
        'Start Date',
        'End Date',
        'Primary Intervention',
        'Secondary Intervention',
        'Notes'
    ])
    
    # Write data rows
    for med in filtered_meds:
        writer.writerow([
            med.get('drug_name', ''),
            med.get('dose', '') if med.get('dose') is not None else '',
            med.get('unit', '') or '',
            med.get('frequency', '') or '',
            med.get('route', '') or '',
            med.get('category', '') or '',
            med.get('indication', '') or '',
            med.get('start_date', ''),
            med.get('end_date', '') or '',
            'Yes' if med.get('is_primary_intervention') == 1 else 'No',
            'Yes' if med.get('is_secondary_intervention') == 1 else 'No',
            med.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename
    today = date.today().isoformat()
    filename = f"medications_{filename_suffix}_{today}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
    
@app.route("/export/events")
def export_events():
    """Export clinical events as CSV within date range and optional event type filter."""
    
    start_date = request.args.get("start")
    end_date = request.args.get("end")
    event_type = request.args.get("type", "all")
    
    if not start_date or not end_date:
        return "Missing date range parameters", 400
    
    # Get all events
    all_events = db.get_clinical_events(uid())
    
    # Filter by date range
    filtered_events = [
        event for event in all_events
        if start_date <= event["date"] <= end_date
    ]
    
    # Filter by event type if not "all"
    if event_type != "all":
        filtered_events = [
            event for event in filtered_events
            if event.get("event_type") == event_type
        ]
    
    # Sort by date (most recent first)
    filtered_events.sort(key=lambda x: x["date"], reverse=True)
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Date',
        'Event Type',
        'Provider',
        'Facility',
        'Follow-up Date',
        'Notes'
    ])
    
    # Write data rows
    for event in filtered_events:
        writer.writerow([
            event.get('date', ''),
            event.get('event_type', ''),
            event.get('provider', '') or '',
            event.get('facility', '') or '',
            event.get('follow_up_date', '') or '',
            event.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename
    type_suffix = event_type if event_type != "all" else "all"
    filename = f"events_{type_suffix}_{start_date}_to_{end_date}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
    
    
    

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

def calculate_model_stats(observations, custom_weights=None):
    """Calculate model accuracy metrics with severity breakdown."""
    true_pos = 0
    true_neg = 0
    false_pos = 0
    false_neg = 0

    # Severity breakdown
    missed_minor = 0
    missed_major = 0
    caught_minor = 0
    caught_major = 0

    # Resolve threshold from custom weights or user prefs
    if custom_weights:
        threshold = custom_weights.get('flare_threshold', 8.0)
    else:
        threshold = get_current_weights(
            current_user.id if current_user.is_authenticated else None
        ).get('flare_threshold', 8.0)

    for obs in observations:
        if custom_weights:
            score = calculate_flare_score_with_weights(obs, custom_weights)
        else:
            score = calculate_flare_prime_score(obs)

        predicted_flare = score >= threshold
        actual_flare = obs.get('flare_occurred') == 1
        severity = obs.get('flare_severity')  # 'minor', 'major', or None

        if predicted_flare and actual_flare:
            true_pos += 1
            if severity == 'major':
                caught_major += 1
            elif severity == 'minor':
                caught_minor += 1
        elif not predicted_flare and not actual_flare:
            true_neg += 1
        elif predicted_flare and not actual_flare:
            false_pos += 1
        else:  # missed flare
            false_neg += 1
            if severity == 'major':
                missed_major += 1
            elif severity == 'minor':
                missed_minor += 1

    total = len(observations)
    correct = true_pos + true_neg

    accuracy = round((correct / total * 100) if total > 0 else 0, 1)

    predicted_pos = true_pos + false_pos
    precision = round((true_pos / predicted_pos * 100) if predicted_pos > 0 else 0, 1)

    actual_pos = true_pos + false_neg
    recall = round((true_pos / actual_pos * 100) if actual_pos > 0 else 0, 1)

    # Major flare recall (most important safety metric)
    total_major = caught_major + missed_major
    major_recall = round((caught_major / total_major * 100) if total_major > 0 else 0, 1)

    return {
        'accuracy': accuracy,
        'precision': precision,
        'recall': recall,
        'true_positives': true_pos,
        'true_negatives': true_neg,
        'false_positives': false_pos,
        'false_negatives': false_neg,
        'missed_minor': missed_minor,
        'missed_major': missed_major,
        'caught_minor': caught_minor,
        'caught_major': caught_major,
        'major_recall': major_recall,
    }

def analyze_prediction_flips(observations, custom_weights):
    """Identify which predictions would change with new weights."""
    flips_to_positive = []
    flips_to_negative = []

    old_threshold = get_current_weights(
        current_user.id if current_user.is_authenticated else None
    ).get('flare_threshold', 8.0)
    new_threshold = custom_weights.get('flare_threshold', old_threshold)

    for obs in observations[:10]:
        old_score = calculate_flare_prime_score(obs)
        new_score = calculate_flare_score_with_weights(obs, custom_weights)

        old_pred = old_score >= old_threshold
        new_pred = new_score >= new_threshold
        
        if not old_pred and new_pred:
            flips_to_positive.append(obs['date'])
        elif old_pred and not new_pred:
            flips_to_negative.append(obs['date'])
    
    summary = ""
    if flips_to_positive:
        summary += f"> Would now predict flare on: {', '.join(flips_to_positive)}<br>"
    if flips_to_negative:
        summary += f"> Would no longer predict flare on: {', '.join(flips_to_negative)}<br>"
    if not summary:
        summary = "> No prediction changes in the last 10 days."
    
    return {'summary': summary}


def assign_grade(accuracy):
    """Assign letter grade."""
    if accuracy >= 85:
        return 'A'
    elif accuracy >= 75:
        return 'B'
    elif accuracy >= 65:
        return 'C'
    elif accuracy >= 50:
        return 'D'
    else:
        return 'F'    
# ============================================================
# Forecast Laboratory
# ============================================================

@app.route("/forecast/lab")
def forecast_lab():
    """
    Experimental model tuning interface.
    Terminal-style UI for adjusting weights and running simulations.
    """
    # Get current model performance
    all_obs = db.get_all_daily_observations(uid())
    if not all_obs or len(all_obs) < 7:
        return redirect(url_for('forecast'))
    
    # Calculate current metrics (reuse from forecast_accuracy)
    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=60)

    analysis_set = all_obs[:60]

    # Calculate current stats
    model_stats = calculate_model_stats(analysis_set)
    
    # Get current weights (from user prefs or defaults)
    current_weights = get_current_weights(current_user.id)
    
    # Check if using custom weights
    using_custom = os.path.exists(CUSTOM_WEIGHTS_PATH)
    
    # Current symptom weights for display
    symptoms = [
        {'key': 'neurological', 'name': 'Neurological', 
         'weight': current_weights['neurological'], 
         'description': 'Numbness, tingling, vision changes'},
        {'key': 'cognitive', 'name': 'Cognitive', 
         'weight': current_weights['cognitive'],
         'description': 'Brain fog, memory, word recall'},
        {'key': 'musculature', 'name': 'Musculature', 
         'weight': current_weights['musculature'],
         'description': 'Muscle pain, cramping, weakness'},
        {'key': 'migraine', 'name': 'Migraine', 
         'weight': current_weights['migraine'],
         'description': 'Headaches, light sensitivity'},
        {'key': 'pulmonary', 'name': 'Pulmonary', 
         'weight': current_weights['pulmonary'],
         'description': 'Air hunger, chest discomfort'},
        {'key': 'dermatological', 'name': 'Dermatological', 
         'weight': current_weights['dermatological'],
         'description': 'Rash, skin changes, photosensitivity'},
        {'key': 'mucosal', 'name': 'Mucosal', 
         'weight': current_weights['mucosal'],
         'description': 'Dry mouth, dry eyes, nasal dryness'},
        {'key': 'rheumatic', 'name': 'Rheumatic (base)',
         'weight': current_weights['rheumatic'],
         'description': 'Joint pain without specificity'},
    ]

    prefs = get_user_prefs()
    if prefs.get('track_cycle', CONFIG.get('track_cycle')):
        symptoms.append({
            'key': 'cycle_phase',
            'name': 'Cycle Phase (PMS/Luteal)',
            'weight': current_weights.get('cycle_phase', 1.0),
            'description': 'Elevated risk during luteal and PMS phases of cycle'
        })
    
    # Model code — pull live source from the actual function
    import inspect
    model_code = inspect.getsource(calculate_flare_prime_score)
    
    # Achievements (check localStorage or session for unlocked ones)
    achievements = [
        {'icon': '🏆', 'name': 'First Experiment', 'unlocked': False,
         'description': 'Adjusted your first weight'},
        {'icon': '📈', 'name': 'Recall Hero', 'unlocked': False,
         'description': 'Improved recall by 10%'},
        {'icon': '🎯', 'name': 'Precision Master', 'unlocked': model_stats['precision'] > 90,
         'description': 'Maintained >90% precision'},
        {'icon': '🧪', 'name': 'Mad Scientist', 'unlocked': False,
         'description': 'Ran 10 simulations'},
        {'icon': '⚖️', 'name': 'Perfect Balance', 'unlocked': False,
         'description': 'Achieved 80%+ accuracy, recall, and precision'},
    ]
    
    # Personal lag correlation summary
    try:
        lag_summary = _compute_personal_lag_summary(current_user.id)
    except Exception:
        lag_summary = None

    return render_template(
        "forecast_lab.html",
        current_accuracy=model_stats['accuracy'],
        current_recall=model_stats['recall'],
        current_precision=model_stats['precision'],
        false_negatives=model_stats['false_negatives'],
        false_positives=model_stats['false_positives'],
        symptoms=symptoms,
        current_weights=current_weights,
        model_code=model_code,
        achievements=achievements,
        manual_text=FORECAST_LAB_MANUAL,
        using_custom=using_custom,
        lag_summary=lag_summary,
    )
    
    

@app.route("/forecast/lab/simulate", methods=["POST"])
def forecast_lab_simulate():
    """
    Run simulation with custom weights.
    Returns new accuracy metrics and which predictions would flip.
    """
    from flask import request, jsonify
    
    custom_weights = request.json.get('weights', {})
    
    # Get data
    all_obs = db.get_all_daily_observations(uid())
    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=60)

    analysis_set = all_obs[:60]

    # Calculate stats with custom weights
    new_stats = calculate_model_stats(analysis_set, custom_weights)
    
    # Calculate stats with current weights (for comparison)
    current_stats = calculate_model_stats(analysis_set, None)
    
    # Find which predictions would flip
    flips = analyze_prediction_flips(analysis_set, custom_weights)
    
    return jsonify({
        'accuracy': new_stats['accuracy'],
        'recall': new_stats['recall'],
        'precision': new_stats['precision'],
        'grade': assign_grade(new_stats['accuracy']),
        'accuracy_change': round(new_stats['accuracy'] - current_stats['accuracy'], 1),
        'recall_change': round(new_stats['recall'] - current_stats['recall'], 1),
        'precision_change': round(new_stats['precision'] - current_stats['precision'], 1),
        'flip_summary': flips['summary']
    })


# ============================================================
# Lab Simulation Apply & Restart
# ============================================================

@app.route("/forecast/lab/apply", methods=["POST"])
def forecast_lab_apply():
    """
    Apply custom weights to the model.
    Saves weights to user preferences and recalculates stats.
    """
    from flask import request, jsonify

    try:
        custom_weights = request.json.get('weights', {})

        # Validate weights
        _category_keys = ('uv_weight', 'exertion_weight', 'temperature_weight', 'pain_fatigue_weight')
        for key, value in custom_weights.items():
            if not isinstance(value, (int, float)):
                return jsonify({'success': False, 'error': f'Invalid weight for {key}'}), 400
            if key == 'flare_threshold':
                if value < 4 or value > 20:
                    return jsonify({'success': False, 'error': f'Invalid weight for {key}'}), 400
            elif key in _category_keys:
                if value < 0 or value > 2:
                    return jsonify({'success': False, 'error': f'Invalid weight for {key}'}), 400
            elif value < 0 or value > 3:
                return jsonify({'success': False, 'error': f'Invalid weight for {key}'}), 400

        # Save to user preferences
        save_custom_weights(custom_weights, user_id=current_user.id)

        # Invalidate cached prefs so get_current_weights reads fresh data
        from flask import g
        if hasattr(g, '_user_prefs'):
            del g._user_prefs

        # Recalculate stats with the just-saved weights
        all_obs = db.get_all_daily_observations(uid())
        all_obs.sort(key=lambda x: x['date'], reverse=True)
        _inject_cycle_phase(all_obs)

        obs_by_date = {o['date']: o for o in all_obs}
        _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=60)

        analysis_set = all_obs[:60]
        new_stats = calculate_model_stats(analysis_set, custom_weights)

        return jsonify({
            'success': True,
            'message': 'Weights applied successfully!',
            'new_accuracy': new_stats['accuracy'],
            'new_recall': new_stats['recall'],
            'new_precision': new_stats['precision']
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/forecast/lab/reset", methods=["POST"])
def forecast_lab_reset():
    """
    Reset to factory default weights.
    Deletes custom config file.
    """
    from flask import jsonify
    
    try:
        reset_to_default_weights(user_id=current_user.id)
        
        return jsonify({
            'success': True,
            'message': 'Reset to factory defaults successfully!'
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# Forecast
# ============================================================
@app.route("/forecast")
def forecast():
    """
    Flare risk forecast page.
    Calculates flare prime score based on recent observations.
    """
    from datetime import datetime, timedelta
    
    # Get last 30 days of observations for analysis
    all_obs = db.get_all_daily_observations(uid())
    if not all_obs:
        return render_template("forecast.html", has_data=False)
    
    # Sort by date
    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    # Need at least 7 days
    if len(all_obs) < 7:
        return render_template("forecast.html", has_data=False)
    
    # Get last 7 days for trend
    last_7 = all_obs[:7]
    today_obs = all_obs[0] if all_obs else None

    if not today_obs:
        return render_template("forecast.html", has_data=False)

    # Inject multi-day scoring context
    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=7)

    # Calculate scores for last 7 days
    scores_7day = []
    for obs in last_7:
        score = calculate_flare_prime_score(obs)
        scores_7day.append({
            'date': obs['date'],
            'score': score
        })
    
    # Today's score with 3-day weighted average
    today_score = scores_7day[0]['score']
    
    # 3-day rolling weighted average (if we have enough data)
    if len(scores_7day) >= 3:
        weighted_score = (
            scores_7day[0]['score'] * 1.0 +  # today
            scores_7day[1]['score'] * 0.75 +  # yesterday
            scores_7day[2]['score'] * 0.5     # day before
        ) / 2.25
    else:
        weighted_score = today_score
    
    # Determine risk level and color (using user's threshold)
    _fw = get_current_weights(uid())
    _threshold = _fw.get('flare_threshold', 8.0)
    risk_info = get_risk_level(weighted_score, _threshold)

    # Get contributing factors (what's adding to score today)
    factors = get_contributing_factors(today_obs)
    
    # Get recommendations based on risk level
    recommendations = get_recommendations(risk_info['level'], factors)
    
    # Build trend data for chart
    trend_data = {
        'dates': [format_date_short(s['date']) for s in reversed(scores_7day)],
        'scores': [s['score'] for s in reversed(scores_7day)]
    }
    
    # Build score breakdown with 7-day component history for sparklines
    COMPONENT_META = [
        ('uv',           'UV Exposure',    '#d4b84a'),
        ('exertion',     'Physical Load',  '#d4a054'),
        ('temperature',  'Temperature',    '#c94040'),
        ('symptoms',     'Symptoms',       '#9b72cf'),
        ('pain_fatigue', 'Pain & Fatigue', '#e85d9e'),
        ('burden_delta', 'Burden Delta',   '#5b9bd5'),
        ('rmssd',        'RMSSD',          '#66bb6a'),
        ('resp_rate',    'Resp Rate',      '#e0a050'),
    ]

    # Compute components for each of the 7 days
    comp_history = [_score_components(obs) for obs in last_7]
    today_comp = comp_history[0]

    breakdown = []
    for key, name, color in COMPONENT_META:
        val = today_comp[key]
        # Skip components that are zero today and have been zero all week
        history = [c[key] for c in reversed(comp_history)]  # chronological
        if val == 0 and all(h == 0 for h in history):
            continue
        breakdown.append({
            'name': name,
            'score': val,
            'color': color,
            'history': history,
        })

    # Score trend delta vs yesterday
    score_delta = round(weighted_score - scores_7day[1]['score'], 1) if len(scores_7day) >= 2 else None

    # Binary prediction (same logic used in accuracy grading)
    predicted_flare = weighted_score >= _threshold

    return render_template(
        "forecast.html",
        has_data=True,
        n_days=len(all_obs),
        today_score=round(weighted_score, 1),
        max_score=25,  # Theoretical maximum
        risk_percentage=min(100, (weighted_score / 25) * 100),
        risk_level=risk_info['level'],
        risk_color=risk_info['color'],
        risk_description=risk_info['description'],
        factors=factors,
        recommendations=recommendations,
        trend_data=trend_data,
        breakdown=breakdown,
        breakdown_json=json.dumps(breakdown),
        score_delta=score_delta,
        predicted_flare=predicted_flare,
        flare_threshold=round(_threshold, 1)
    )

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
    
def get_recommendations(risk_level: str, factors: list) -> list:
    """Generate actionable recommendations based on risk."""
    recs = []
    
    if risk_level == 'Low Risk':
        recs.append({'icon': '✓', 'text': 'Maintain current routine and rest schedule'})
        recs.append({'icon': '☀', 'text': 'Continue with normal sun protection practices'})
        recs.append({'icon': '⛆', 'text': 'Stay hydrated and maintain balanced nutrition'})
    
    elif risk_level == 'Moderate Risk':
        recs.append({'icon': '⚠', 'text': 'Reduce physical demands and pace activities'})
        recs.append({'icon': '☀', 'text': 'Limit UV exposure, stay in shade during peak hours'})
        recs.append({'icon': '⏾', 'text': 'Prioritize 8+ hours of sleep tonight'})
        recs.append({'icon': '❆', 'text': 'Use cooling strategies if overheated'})
    
    elif risk_level == 'High Risk':
        recs.append({'icon': '⚠', 'text': 'Avoid strenuous activity and sun exposure'})
        recs.append({'icon': '⏾', 'text': 'Rest is critical - cancel non-essential plans'})
        recs.append({'icon': '℞', 'text': 'Have NSAIDs and comfort measures ready'})
        recs.append({'icon': '⦨', 'text': 'Monitor temperature and symptoms closely'})
    
    else:  # Critical Risk
        recs.append({'icon': '𝚾𝚾𝚾𝚾', 'text': 'Take a full rest day - no exceptions'})
        recs.append({'icon': '⌂', 'text': 'Stay indoors in cool, comfortable environment'})
        recs.append({'icon': '℞', 'text': 'Use all available symptom management tools'})
        recs.append({'icon': '✆', 'text': 'Consider contacting healthcare provider if symptoms worsen'})
    
    # Add specific recommendations based on factors
    factor_names = [f['name'] for f in factors]
    if any('UV' in name for name in factor_names):
        recs.append({'icon': '♛', 'text': 'Wear protective clothing and broad-spectrum sunscreen if going outside'})
    if any('joint' in name.lower() for name in factor_names):
        recs.append({'icon': '❄', 'text': 'Apply cold therapy to affected joints'})
    
    return recs[:5]  # Limit to 5 recommendations


def format_date_short(date_str: str) -> str:
    """Format date as 'Mar 4' for chart labels."""
    from datetime import datetime
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.strftime('%b %d')

# ============================================================
# Forecast History
# ============================================================

@app.route("/forecast/history")
def forecast_history():
    """Show past N days of predictions vs actuals (N = days query param)."""

    days_param = request.args.get('days', '30')

    all_obs = db.get_all_daily_observations(uid())
    if not all_obs:
        return redirect(url_for('forecast'))

    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}

    if days_param == 'all':
        analysis_set = all_obs
        days_display = 'all'
        _inject_scoring_context(all_obs, obs_by_date, get_location_key())
    else:
        try:
            days_int = int(days_param)
        except ValueError:
            days_int = 30
        days_int = max(1, min(days_int, len(all_obs)))
        _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=days_int)
        analysis_set = all_obs[:days_int]
        days_display = days_int

    _hist_weights = get_current_weights(uid())
    _hist_threshold = _hist_weights.get('flare_threshold', 8.0)

    history = []
    correct = 0
    false_pos = 0
    false_neg = 0

    for obs in analysis_set:
        score = calculate_flare_prime_score(obs)
        risk_info = get_risk_level(score, _hist_threshold)

        # Did a flare occur?
        flare_occurred = obs.get('flare_occurred') == 1

        # Did we predict high risk?
        predicted_high = score >= _hist_threshold

        # Check if prediction was correct
        if predicted_high and flare_occurred:
            correct += 1
            prediction_correct = True
        elif not predicted_high and not flare_occurred:
            correct += 1
            prediction_correct = True
        elif predicted_high and not flare_occurred:
            false_pos += 1
            prediction_correct = False
        elif not predicted_high and flare_occurred:
            false_neg += 1
            prediction_correct = False
        else:
            prediction_correct = None
        
        # Get top contributing factors
        factors = get_contributing_factors(obs)
        top_factors = ', '.join([f['name'] for f in factors[:3]]) if factors else 'None'
        
        history.append({
            'date': obs['date'],
            'score': round(score, 1),
            'gap': round(score - _hist_threshold, 1),  # positive = above threshold, negative = below
            'risk_level': risk_info['level'],
            'risk_color': risk_info['color'],
            'flare_occurred': flare_occurred,
            'flare_severity': obs.get('flare_severity') if flare_occurred else None,
            'predicted_high_risk': predicted_high,
            'prediction_correct': prediction_correct,
            'top_factors': top_factors
        })

    # Calculate accuracy
    total = len(analysis_set)
    accuracy = round((correct / total * 100) if total > 0 else 0, 1)

    # Date range for subtitle
    if history:
        date_range = f"{history[-1]['date']} to {history[0]['date']}"
    else:
        date_range = ''

    return render_template(
        "forecast_history.html",
        history=history,
        correct_predictions=correct,
        false_positives=false_pos,
        false_negatives=false_neg,
        accuracy_percent=accuracy,
        days=days_display,
        threshold=_hist_threshold,
        date_range=date_range,
        n_days=len(analysis_set),
    )
    
# ============================================================
# Forecast Accuracy Analysis and Self-Grading
# ============================================================

@app.route("/forecast/accuracy")
def forecast_accuracy():
    """
    Analyze model accuracy and suggest weight adjustments.
    Self-grading system that learns from false predictions.
    """
    from collections import Counter
    
    # Get requested time window
    days_param = request.args.get('days', '60')
    
    # Get all observations
    all_obs = db.get_all_daily_observations(uid())
    if not all_obs:
        return redirect(url_for('forecast'))
    
    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key())

    # Select analysis window
    if days_param == 'all':
        analysis_set = all_obs
        days_display = 'all'
    else:
        days_int = int(days_param)
        analysis_set = all_obs[:days_int]
        days_display = days_int

    # Calculate predictions vs actuals
    true_positives = 0   # Predicted flare, flare occurred
    true_negatives = 0   # Predicted no flare, no flare
    false_positives = 0  # Predicted flare, no flare (false alarm)
    false_negatives = 0  # Predicted no flare, but flare occurred (missed)

    _acc_weights = get_current_weights(uid())
    _acc_threshold = _acc_weights.get('flare_threshold', 8.0)

    # Per-severity counters
    caught_minor = caught_major = caught_er = caught_unspec = 0
    missed_minor = missed_major = missed_er = missed_unspec = 0

    # Factor appearance on caught vs missed majors (for signal quality comparison)
    caught_major_factors = Counter()
    missed_major_factors = Counter()
    false_pos_factors = Counter()

    # Full ranked lists (not capped)
    missed_majors = []   # major + er lumped here, tagged by severity
    missed_minors = []
    false_alarms = []

    def _case(obs, score, factors):
        return {
            'date': obs['date'],
            'score': round(score, 1),
            'gap': round(_acc_threshold - score, 1),  # how far below threshold (positive = worse miss)
            'factors': ', '.join([f['name'] for f in factors[:3]]) if factors else '(none fired)',
            'notes': (obs.get('notes') or '').strip()[:200],
            'severity': obs.get('flare_severity') or 'unspecified',
            'pain': obs.get('pain_scale'),
            'fatigue': obs.get('fatigue_scale'),
        }

    for obs in analysis_set:
        score = calculate_flare_prime_score(obs)
        predicted_flare = score >= _acc_threshold
        actual_flare = obs.get('flare_occurred') == 1
        severity = obs.get('flare_severity')
        factors = get_contributing_factors(obs)

        if predicted_flare and actual_flare:
            true_positives += 1
            if severity == 'er_visit':
                caught_er += 1
                for f in factors: caught_major_factors[f['name']] += 1
            elif severity == 'major':
                caught_major += 1
                for f in factors: caught_major_factors[f['name']] += 1
            elif severity == 'minor':
                caught_minor += 1
            else:
                caught_unspec += 1
        elif not predicted_flare and not actual_flare:
            true_negatives += 1
        elif predicted_flare and not actual_flare:
            false_positives += 1
            for f in factors:
                false_pos_factors[f['name']] += 1
            false_alarms.append(_case(obs, score, factors))
        elif not predicted_flare and actual_flare:
            false_negatives += 1
            if severity == 'er_visit':
                missed_er += 1
                missed_majors.append(_case(obs, score, factors))
                for f in factors: missed_major_factors[f['name']] += 1
            elif severity == 'major':
                missed_major += 1
                missed_majors.append(_case(obs, score, factors))
                for f in factors: missed_major_factors[f['name']] += 1
            elif severity == 'minor':
                missed_minor += 1
                missed_minors.append(_case(obs, score, factors))
            else:
                missed_unspec += 1
                missed_minors.append(_case(obs, score, factors))

    # Rank missed lists worst-first (largest gap = worst miss)
    missed_majors.sort(key=lambda c: -c['gap'])
    missed_minors.sort(key=lambda c: -c['gap'])
    # False alarms: largest score first (most confident wrong)
    false_alarms.sort(key=lambda c: -c['score'])

    # Calculate metrics
    total = len(analysis_set)
    correct = true_positives + true_negatives
    accuracy = round((correct / total * 100) if total > 0 else 0, 1)

    # Precision: Of all predicted flares, how many were correct?
    predicted_pos = true_positives + false_positives
    precision = round((true_positives / predicted_pos * 100) if predicted_pos > 0 else 0, 1)

    # Combined recall: Of all actual flares, how many did we catch?
    actual_pos = true_positives + false_negatives
    recall = round((true_positives / actual_pos * 100) if actual_pos > 0 else 0, 1)

    # Per-severity recall — major is the primary metric (function-limiting flares)
    major_total = caught_major + missed_major + caught_er + missed_er
    minor_total = caught_minor + missed_minor
    major_recall = round((caught_major + caught_er) / major_total * 100, 1) if major_total else None
    minor_recall = round(caught_minor / minor_total * 100, 1) if minor_total else None

    # False alarm rate
    predicted_pos_total = true_positives + false_positives
    false_alarm_rate = round((false_positives / predicted_pos_total * 100) if predicted_pos_total > 0 else 0, 1)

    # Factor signal quality: for each factor, compare appearance rate on caught vs missed majors
    # (higher = factor correctly discriminates; lower = factor is absent when we need it)
    factor_signal = []
    all_major_factors = set(caught_major_factors) | set(missed_major_factors)
    caught_major_n = caught_major + caught_er
    missed_major_n = missed_major + missed_er
    for fname in all_major_factors:
        c = caught_major_factors.get(fname, 0)
        m = missed_major_factors.get(fname, 0)
        c_rate = (c / caught_major_n * 100) if caught_major_n else 0
        m_rate = (m / missed_major_n * 100) if missed_major_n else 0
        factor_signal.append({
            'factor': fname,
            'caught_rate': round(c_rate, 0),
            'missed_rate': round(m_rate, 0),
            'caught_count': c,
            'missed_count': m,
            'lift': round(c_rate - m_rate, 0),  # positive = fires more on caught than missed
        })
    # Sort: most useful discriminators first (factor that fires on caught but NOT missed)
    factor_signal.sort(key=lambda x: -x['lift'])

    # Generate weight adjustment suggestions (unchanged logic, now uses severity-aware signals)
    suggestions = []
    if false_positives > 5:
        for factor, count in false_pos_factors.most_common(3):
            if count >= 3:
                suggestions.append({
                    'factor': factor,
                    'current_weight': 'Current',
                    'suggested_weight': '↓ Reduce',
                    'reason': f'Appears in {count} false alarms. May be over-weighted.',
                    'color': '#d4784a'
                })
    if missed_major + missed_er > 0:
        # Suggest based on factors that fire on caught but not missed majors
        for fs in factor_signal[:3]:
            if fs['lift'] >= 25 and fs['missed_count'] == 0:
                suggestions.append({
                    'factor': fs['factor'],
                    'current_weight': 'Current',
                    'suggested_weight': '↑ Increase OR lower threshold',
                    'reason': f"Fires on {fs['caught_count']}/{caught_major_n} caught majors but 0/{missed_major_n} missed ones — strong discriminator.",
                    'color': '#c94040'
                })
    if major_recall is not None and major_recall < 70:
        suggestions.insert(0, {
            'factor': 'Major Flare Recall',
            'current_weight': f'{major_recall}%',
            'suggested_weight': 'Lower threshold or add pain/fatigue ladders',
            'reason': f'Missing {missed_major + missed_er} of {major_total} major flares. Function-limiting days are the most important to catch.',
            'color': '#c94040'
        })

    return render_template(
        "forecast_accuracy.html",
        n_days=len(analysis_set),
        days=days_display,
        threshold=_acc_threshold,
        accuracy=accuracy,
        precision=precision,
        recall=recall,
        major_recall=major_recall,
        minor_recall=minor_recall,
        false_alarm_rate=false_alarm_rate,
        correct_predictions=correct,
        total_predictions=total,
        true_positives=true_positives,
        true_negatives=true_negatives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        suggestions=suggestions,
        missed_majors=missed_majors,
        missed_minors=missed_minors,
        false_alarms=false_alarms,
        missed_minor=missed_minor,
        missed_major=missed_major,
        missed_er=missed_er,
        missed_unspec=missed_unspec,
        caught_minor=caught_minor,
        caught_major=caught_major,
        caught_er=caught_er,
        caught_unspec=caught_unspec,
        major_total=major_total,
        minor_total=minor_total,
        factor_signal=factor_signal,
    )

# ============================================================
# Pre-Flare Pattern Analysis
# ============================================================

_PATTERN_SYMPTOMS = [
    'neurological', 'cognitive', 'musculature', 'migraine',
    'pulmonary', 'dermatological', 'rheumatic', 'mucosal', 'gastro',
]
_PATTERN_SYMPTOM_LABELS = {
    'neurological': 'Neurological', 'cognitive': 'Cognitive',
    'musculature': 'Musculature', 'migraine': 'Migraine',
    'pulmonary': 'Pulmonary', 'dermatological': 'Dermatological',
    'rheumatic': 'Rheumatic', 'mucosal': 'Mucosal', 'gastro': 'GI',
}

@app.route("/forecast/patterns")
@login_required
def forecast_patterns():
    """Pre-flare pattern analysis — what do the days before severe events look like?"""
    all_obs = db.get_all_daily_observations(uid())
    if not all_obs:
        return render_template("forecast_patterns.html", has_data=False)

    all_obs.sort(key=lambda x: x['date'])
    _inject_cycle_phase(all_obs)
    obs_by_date = {o['date']: o for o in all_obs}

    # Group flares by severity
    flare_dates = {'er_visit': [], 'major': [], 'minor': [], 'unlabeled': []}
    for obs in all_obs:
        if obs.get('flare_occurred') == 1:
            sev = obs.get('flare_severity') or 'unlabeled'
            if sev in flare_dates:
                flare_dates[sev].append(obs['date'])
            else:
                flare_dates['unlabeled'].append(obs['date'])

    # Build pre-flare windows (3 days before each flare)
    lookback = 3

    def _build_window(flare_date_str):
        """Get observations for the N days before a flare date."""
        target = date.fromisoformat(flare_date_str)
        window = []
        for offset in range(1, lookback + 1):
            d = (target - timedelta(days=offset)).isoformat()
            obs = obs_by_date.get(d)
            if obs:
                window.append(obs)
        return window

    # Get location key for UV lookups
    loc_key = get_location_key()

    def _profile_windows(windows):
        """Compute average biometrics and symptom frequencies across windows."""
        if not windows:
            return None

        all_obs_in_windows = [obs for w in windows for obs in w]
        n = len(all_obs_in_windows)
        if n == 0:
            return None

        # Biometric averages
        def _avg(key):
            vals = [float(o[key]) for o in all_obs_in_windows if o.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        # UV metrics: raw weighted UV index and computed UV dose per day
        uv_indices = []
        uv_doses = []
        for o in all_obs_in_windows:
            uv_row = db.get_uv_data(loc_key, o['date'])
            if uv_row:
                w_uv = weighted_uv(uv_row)
                uv_indices.append(w_uv)
                sun_min = float(o.get('sun_exposure_min') or 0)
                protection = UV_PROTECTION_MULTIPLIERS.get(
                    o.get('uv_protection_level') or 'none', 1.0)
                uv_doses.append((w_uv ** 1.5) * sun_min * protection)
        uv_index_avg = round(sum(uv_indices) / len(uv_indices), 2) if uv_indices else None
        uv_dose_avg = round(sum(uv_doses) / len(uv_doses), 1) if uv_doses else None

        # Symptom frequency (% of pre-flare days with each symptom)
        symptom_freq = {}
        for sym in _PATTERN_SYMPTOMS:
            count = sum(1 for o in all_obs_in_windows if o.get(sym))
            symptom_freq[sym] = round(count / n * 100, 1)

        # Top symptom combos (which symptoms co-occur in the same day)
        combos = {}
        for o in all_obs_in_windows:
            active = tuple(sorted(s for s in _PATTERN_SYMPTOMS if o.get(s)))
            if active:
                combos[active] = combos.get(active, 0) + 1
        top_combos = sorted(combos.items(), key=lambda x: -x[1])[:5]

        # Trajectory: compare day-3 vs day-1 for fatigue and pain
        day1_fatigue = []
        day3_fatigue = []
        day1_pain = []
        day3_pain = []
        for w in windows:
            if len(w) >= 1 and w[0].get('fatigue_scale') is not None:
                day1_fatigue.append(float(w[0]['fatigue_scale']))
            if len(w) >= 3 and w[2].get('fatigue_scale') is not None:
                day3_fatigue.append(float(w[2]['fatigue_scale']))
            if len(w) >= 1 and w[0].get('pain_scale') is not None:
                day1_pain.append(float(w[0]['pain_scale']))
            if len(w) >= 3 and w[2].get('pain_scale') is not None:
                day3_pain.append(float(w[2]['pain_scale']))

        def _safe_avg(lst):
            return round(sum(lst) / len(lst), 1) if lst else None

        # Cycle phase distribution
        phase_counts = {'pms': 0, 'luteal': 0, 'follicular': 0}
        for o in all_obs_in_windows:
            ph = o.get('cycle_phase_name')
            if ph in ('pms', 'luteal'):
                phase_counts[ph] += 1
            else:
                phase_counts['follicular'] += 1
        phase_pct = {k: round(v / n * 100, 1) for k, v in phase_counts.items()} if n else {}

        return {
            'n_flares': len(windows),
            'n_obs': n,
            'fatigue_avg': _avg('fatigue_scale'),
            'pain_avg': _avg('pain_scale'),
            'hrv_avg': _avg('hrv'),
            'rmssd_avg': _avg('hrv_rmssd'),
            'rhr_avg': _avg('resting_heart_rate'),
            'spo2_avg': _avg('spo2'),
            'resp_rate_avg': _avg('respiratory_rate'),
            'bbt_avg': _avg('basal_temp_delta'),
            'sleep_avg': _avg('hours_slept'),
            'steps_avg': _avg('steps'),
            'uv_index_avg': uv_index_avg,
            'uv_dose_avg': uv_dose_avg,
            'symptom_freq': symptom_freq,
            'top_combos': [
                {'symptoms': [_PATTERN_SYMPTOM_LABELS.get(s, s) for s in combo], 'count': cnt}
                for combo, cnt in top_combos
            ],
            'fatigue_trajectory': {
                'day3': _safe_avg(day3_fatigue),
                'day1': _safe_avg(day1_fatigue),
            },
            'pain_trajectory': {
                'day3': _safe_avg(day3_pain),
                'day1': _safe_avg(day1_pain),
            },
            'phase_pct': phase_pct,
        }

    # Build profiles for each severity tier
    profiles = {}
    for sev in ('er_visit', 'major', 'minor'):
        windows = [_build_window(d) for d in flare_dates[sev]]
        windows = [w for w in windows if w]  # drop empty windows
        profiles[sev] = _profile_windows(windows)

    # Baseline: sample non-flare days (every 7th day that's not within 3 days of a flare)
    all_flare_dates = set()
    for dates_list in flare_dates.values():
        for fd in dates_list:
            target = date.fromisoformat(fd)
            for offset in range(-3, 4):
                all_flare_dates.add((target + timedelta(days=offset)).isoformat())

    baseline_windows = []
    non_flare_obs = [o for o in all_obs if o['date'] not in all_flare_dates]
    for i in range(0, len(non_flare_obs), 7):
        w = _build_window(non_flare_obs[i]['date'])
        if w:
            baseline_windows.append(w)
    profiles['baseline'] = _profile_windows(baseline_windows)

    # Count totals for display
    flare_counts = {sev: len(dates) for sev, dates in flare_dates.items()}

    # Build 7-day RMSSD trajectories for severe events (ER + major)
    rmssd_trajectories = []
    for sev in ('er_visit', 'major'):
        for flare_date_str in flare_dates[sev]:
            target = date.fromisoformat(flare_date_str)
            window = []
            for offset in range(7, -1, -1):  # day -7 through day 0
                d = (target - timedelta(days=offset)).isoformat()
                obs = obs_by_date.get(d)
                rmssd = round(float(obs['hrv_rmssd']), 2) if obs and obs.get('hrv_rmssd') is not None else None
                window.append(rmssd)
            rmssd_trajectories.append({
                'date': flare_date_str,
                'severity': sev,
                'values': window,
            })

    has_rmssd_trajectories = any(
        any(v is not None for v in t['values']) for t in rmssd_trajectories
    )

    # Baseline RMSSD average on non-flare days (for reference line)
    baseline_rmssd_vals = [
        float(o['hrv_rmssd']) for o in non_flare_obs
        if o.get('hrv_rmssd') is not None
    ]
    baseline_rmssd = (
        round(sum(baseline_rmssd_vals) / len(baseline_rmssd_vals), 2)
        if baseline_rmssd_vals else None
    )

    # Aggregate RMSSD trajectory stats (mean, std, n per day-offset)
    agg_rmssd = {'mean': [], 'std': [], 'n': []}
    for i in range(8):  # 8 data points: day -7 through day 0
        vals = [
            t['values'][i] for t in rmssd_trajectories
            if t['values'][i] is not None
        ]
        if vals:
            m = round(sum(vals) / len(vals), 2)
            agg_rmssd['mean'].append(m)
            agg_rmssd['std'].append(
                round(statistics.stdev(vals), 2) if len(vals) >= 2 else 0
            )
            agg_rmssd['n'].append(len(vals))
        else:
            agg_rmssd['mean'].append(None)
            agg_rmssd['std'].append(None)
            agg_rmssd['n'].append(0)

    # Trend: linear slope of aggregate RMSSD means (ms per day)
    rmssd_trend = None
    valid_points = [
        (i, agg_rmssd['mean'][i]) for i in range(8)
        if agg_rmssd['mean'][i] is not None
    ]
    if len(valid_points) >= 3:
        xs = [p[0] for p in valid_points]
        ys = [p[1] for p in valid_points]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        den = sum((x - x_mean) ** 2 for x in xs)
        if den > 0:
            slope = round(num / den, 2)
            total_change = round(slope * 7, 1)
            if abs(slope) < 0.3:
                direction = 'flat'
            elif slope > 0:
                direction = 'rising'
            else:
                direction = 'falling'
            rmssd_trend = {
                'direction': direction,
                'slope': slope,
                'total_change': total_change,
            }

    return render_template(
        "forecast_patterns.html",
        has_data=True,
        profiles=profiles,
        flare_counts=flare_counts,
        symptom_labels=_PATTERN_SYMPTOM_LABELS,
        symptom_keys=_PATTERN_SYMPTOMS,
        rmssd_trajectories_json=json.dumps(rmssd_trajectories),
        has_rmssd_trajectories=has_rmssd_trajectories,
        rmssd_aggregate_json=json.dumps(agg_rmssd),
        baseline_rmssd=baseline_rmssd,
        rmssd_trend=rmssd_trend,
    )


# ============================================================
# Search
# ============================================================

@app.route("/search", methods=["GET", "POST"])
def search():
    """Search through observations and clinical notes."""
    
    # Get query from either GET or POST
    query = request.args.get("q", "").strip() or request.form.get("query", "").strip()
    
    # Easter egg: redirect to lab for help queries
    if query.lower() in ['help', 'user manual', 'cli', 'lab', 'code', 'weights', 'tune', 'manual']:
        return redirect(url_for('forecast_lab'))

    grouped = {
        "daily":       [],
        "labs":        [],
        "events":      [],
        "medications": [],
    }
    total = 0

    # Always fetch full dataset for report summary and chart
    all_observations = db.get_all_daily_observations(uid())
    all_meds         = db.get_all_medications(uid())

    tracking_start = all_observations[0]["date"] if all_observations else None
    tracking_end   = all_observations[-1]["date"] if all_observations else None

    today_str = date.today().isoformat()
    active_meds = [
        m for m in all_meds
        if m["start_date"] <= today_str and
           (m.get("end_date") is None or m["end_date"] >= today_str)
    ]

    uv_all = []
    if tracking_start and tracking_end:
        uv_all = db.get_uv_data_range(get_location_key(), tracking_start, tracking_end)

    chart_dataset = {
        "dates": [o["date"] for o in all_observations],
        "sleep": [o.get("hours_slept") for o in all_observations],
        "bbt":   [o.get("basal_temp_delta") for o in all_observations],
        "uv":    {u["date"]: u.get("uv_noon") for u in uv_all},
    }

    if query:
        q = query.lower()

        # Daily entries
        for o in all_observations:
            fields = [
                o.get("notes") or "",
                o.get("neuro_notes") or "",
                o.get("cognitive_notes") or "",
                o.get("musculature_notes") or "",
                o.get("migraine_notes") or "",
                o.get("air_hunger_notes") or "",
                o.get("derm_notes") or "",
                o.get("emotional_notes") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                snippet = next(
                    (f.strip() for f in fields if q in f.lower() and f.strip()),
                    ""
                )
                grouped["daily"].append({
                    "id":      f"daily_{o['date']}",
                    "date":    o["date"],
                    "type":    "daily",
                    "title":   "daily entry",
                    "snippet": snippet[:200] if snippet else combined[:200],
                    "pain":    o.get("pain_scale"),
                    "fatigue": o.get("fatigue_scale"),
                })
                total += 1

        # Lab results
        labs = db.get_lab_results(uid())
        for lab in labs:
            fields = [
                lab.get("test_name") or "",
                lab.get("notes") or "",
                lab.get("provider") or "",
                lab.get("lab_facility") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                val = (f"{lab['numeric_value']} {lab['unit'] or ''}".strip()
                       if lab.get("numeric_value") is not None
                       else lab.get("qualitative_result") or "")
                grouped["labs"].append({
                    "id":      f"lab_{lab['id']}",
                    "date":    lab["date"],
                    "type":    "lab",
                    "title":   lab["test_name"],
                    "snippet": f"{val} — {lab.get('notes') or lab.get('provider') or ''}".strip(" —"),
                })
                total += 1

        # Clinical events
        events = db.get_clinical_events(uid())
        for e in events:
            fields = [
                e.get("notes") or "",
                e.get("provider") or "",
                e.get("facility") or "",
                e.get("event_type") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                snippet = next(
                    (f.strip() for f in fields if q in f.lower() and f.strip()),
                    ""
                )
                grouped["events"].append({
                    "id":      f"event_{e['id']}",
                    "date":    e["date"],
                    "type":    "event",
                    "title":   f"{e['event_type']} — {e.get('provider') or e.get('facility') or ''}".strip(" —"),
                    "snippet": snippet[:200],
                })
                total += 1

        # Medications
        for med in all_meds:
            fields = [
                med.get("drug_name") or "",
                med.get("indication") or "",
                med.get("notes") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                dose_str = f"{med.get('dose') or ''} {med.get('unit') or ''} {med.get('frequency') or ''}".strip()
                grouped["medications"].append({
                    "id":      f"med_{med['id']}",
                    "date":    med["start_date"],
                    "type":    "medication",
                    "title":   med["drug_name"],
                    "snippet": f"{dose_str} — {med.get('indication') or ''}".strip(" —"),
                })
                total += 1

        for key in grouped:
            grouped[key].sort(key=lambda x: x["date"], reverse=True)

    return render_template(
        "search.html",
        query=query,
        grouped=grouped,
        total=total,
        tracking_start=tracking_start,
        tracking_end=tracking_end,
        active_meds=active_meds,
        chart_dataset_json=json.dumps(chart_dataset),
        patient_name=get_user_prefs().get("patient_name") or CONFIG.get("patient_name", ""),
    )

# ============================================================
# Data Management & Export
# ============================================================

@app.route("/export/all-data")
def export_all_data():
    """Export complete database and all data as ZIP file."""
    from io import BytesIO

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # 1. Include SQLite database
        db_path = Path("biotracking.db")
        if db_path.exists():
            zipf.write(str(db_path), "biotracking.db")

        # 2. Export all tables as CSV into the ZIP
        _export_csvs_to_zip(zipf)

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'biotracking_backup_{timestamp}.zip'
    )


def _export_csvs_to_zip(zipf):
    """Export all database tables as CSV strings into a ZipFile."""

    def make_csv(data: list, columns: list) -> str:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in data:
            writer.writerow([row.get(col, '') for col in columns])
        return output.getvalue()

    # Daily observations — all columns
    daily_obs = db.get_all_observations(uid())
    zipf.writestr("daily_observations.csv", make_csv(daily_obs, [
        'date', 'steps', 'hours_slept', 'hrv', 'hrv_rmssd',
        'resting_heart_rate', 'spo2', 'respiratory_rate',
        'basal_temp_delta', 'sun_exposure_min', 'uv_protection_level', 'stayed_indoors',
        'pain_scale', 'fatigue_scale', 'emotional_state', 'emotional_notes',
        'neurological', 'neuro_notes', 'cognitive', 'cognitive_notes',
        'musculature', 'musculature_notes', 'migraine', 'migraine_notes',
        'pulmonary', 'pulmonary_notes', 'dermatological', 'derm_notes',
        'rheumatic', 'rheumatic_notes', 'mucosal', 'mucosal_notes',
        'gastro', 'gastro_notes',
        'air_hunger', 'air_hunger_notes', 'word_loss', 'word_loss_notes',
        'period_flow', 'cramping', 'cycle_notes',
        'flare_occurred', 'flare_severity',
        'strike_physical', 'strike_environmental',
        'notes',
    ]))

    # Labs
    zipf.writestr("labs.csv", make_csv(db.get_lab_results(uid()), [
        'date', 'test_name', 'numeric_value', 'unit', 'qualitative_result',
        'reference_range', 'flag', 'provider', 'lab_facility', 'notes'
    ]))

    # Medications
    zipf.writestr("medications.csv", make_csv(db.get_all_medications(uid()), [
        'drug_name', 'dose', 'unit', 'frequency', 'route', 'category',
        'indication', 'start_date', 'end_date', 'is_primary_intervention',
        'is_secondary_intervention', 'notes'
    ]))

    # Events
    zipf.writestr("events.csv", make_csv(db.get_clinical_events(uid()), [
        'date', 'event_type', 'provider', 'facility', 'follow_up_date', 'notes'
    ]))

    # Clinicians
    zipf.writestr("clinicians.csv", make_csv(db.get_all_clinicians(uid()), [
        'name', 'specialty', 'clinic_name', 'phone', 'email', 'network',
        'address', 'notes'
    ]))

    # ANA results
    zipf.writestr("ana_results.csv", make_csv(db.get_ana_results(uid()), [
        'date', 'titer', 'screen_result', 'patterns', 'provider', 'notes'
    ]))

    # Remaining user-scoped tables — complete (all-column) dumps so the export
    # is truly "everything", not just the human-friendly curated views above.
    # The raw .db is already in the zip; these make the data portable as CSV
    # too. Generic dumper keeps this list the single place to extend.
    for table, filename in [
        ("medication_events",  "medication_events.csv"),
        ("bc_history",         "bc_history.csv"),
        ("taper_schedules",    "taper_schedules.csv"),
        ("scheduled_doses",    "scheduled_doses.csv"),
        ("uv_sensor_readings", "uv_sensor_readings.csv"),
        ("health_sync_events", "health_sync_events.csv"),
        ("user_preferences",   "user_preferences.csv"),
    ]:
        dump = db.export_table_for_user(table, uid())
        zipf.writestr(filename, make_csv(dump["rows"], dump["columns"]))

# ============================================================
# Clinical Report
# ============================================================

def generate_findings(observations, uv_data, start_date, end_date, n_obs=None, user_id=None):
    """Auto-generate clinical findings from data.

    user_id defaults to the logged-in user; pass it explicitly (e.g. from the
    read-only portal, which has no session) to scope the medication lookup.
    """
    import numpy as np
    from scipy import stats

    findings = []
    if n_obs is None:
        n_obs = len(observations)

    # UV lag correlation for period
    if len(observations) >= 10 and len(uv_data) >= 10:
        obs_by_date = {o["date"]: o for o in observations}
        uv_by_date  = {u["date"]: u for u in uv_data}

        dates_with_both = [d for d in obs_by_date
                           if d in uv_by_date and uv_by_date[d].get("uv_noon")]

        if len(dates_with_both) >= 10:
            uv_vals = []
            muscle_vals = []
            for d in dates_with_both:
                uv = uv_by_date[d].get("uv_noon")
                muscle = obs_by_date[d].get("musculature")
                if uv is not None and muscle is not None:
                    uv_vals.append(float(uv))
                    muscle_vals.append(float(muscle))

            if len(uv_vals) >= 8:
                r, p = stats.pearsonr(np.array(uv_vals), np.array(muscle_vals))
                if p < 0.01 and abs(r) >= 0.15:
                    findings.append({
                        "type": "uv_correlation",
                        "text": f"UV exposure shows significant same-day correlation with musculature symptoms (r={r:.3f}, p={p:.4f}, n={len(uv_vals)})."
                    })

    # Flare frequency
    if observations:
        flare_n = sum(1 for o in observations if o.get('flare_occurred') == 1)
        period_days = max(
            (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days, 1
        )
        per_month = round(flare_n / period_days * 30, 1)
        if flare_n > 0:
            findings.append({
                'type': 'flare_frequency',
                'text': f'{flare_n} flare{"s" if flare_n != 1 else ""} recorded in this period '
                        f'({per_month}/month over {period_days} days).'
            })
        else:
            findings.append({'type': 'flare_frequency', 'text': 'No flares recorded in this period.'})

    # Highest-burden symptom category
    if observations and n_obs:
        sym_counts = {
            key: sum(1 for o in observations if o.get(key))
            for key in ['neurological', 'cognitive', 'musculature', 'migraine',
                        'pulmonary', 'dermatological', 'rheumatic', 'gastro', 'mucosal']
        }
        labels = {
            'neurological': 'Neurological', 'cognitive': 'Cognitive',
            'musculature': 'Musculature', 'migraine': 'Migraine',
            'pulmonary': 'Pulmonary', 'dermatological': 'Dermatological',
            'rheumatic': 'Rheumatic', 'gastro': 'Gastrointestinal', 'mucosal': 'Mucosal'
        }
        top = max(sym_counts, key=sym_counts.get)
        top_pct = round(sym_counts[top] / n_obs * 100)
        if sym_counts[top] > 0:
            findings.append({
                'type': 'symptom_burden',
                'text': f'{labels[top]} symptoms were the most frequently reported category, '
                        f'present on {sym_counts[top]} of {n_obs} days ({top_pct}%).'
            })

        # Neurological involvement — flag for rheumatology
        neuro_n = sym_counts.get('neurological', 0)
        neuro_pct = round(neuro_n / n_obs * 100)
        if neuro_pct >= 10:
            findings.append({
                'type': 'neurological',
                'text': f'Neurological symptoms present on {neuro_n} of {n_obs} days ({neuro_pct}%). '
                        f'This may warrant neurology consultation or expanded ANA panel.'
            })

    # Medications started during this period
    all_meds = db.get_all_medications(user_id if user_id is not None else uid())
    meds_started = [m for m in all_meds if start_date <= m['start_date'] <= end_date]
    for med in meds_started:
        dose_str = f"{med.get('dose', '') or ''} {med.get('unit', '') or ''}".strip()
        indication = f" — {med['indication']}" if med.get('indication') else ''
        findings.append({
            'type': 'medication_change',
            'text': f"{med['drug_name']}{(' ' + dose_str) if dose_str else ''} "
                    f"started {med['start_date']}{indication}."
        })

    return findings

# ============================================================
# UV correlated report generation
# ============================================================

# --- Curated lab selection for the clinical report -------------------------
# "Clinically meaningful marker," not "any abnormal." Core markers are always
# surfaced (most recent of each, ALL-TIME — never clipped to the report window,
# since the lupus band / complement / D-dimer that prove a case are usually
# older than 90 days). A second "watch" set surfaces ONLY when out of range
# (anomalous lipids and WBC-differential shifts like lymphopenia/eosinopenia
# are SARD-relevant). Everything else is excluded.
# Spelling variants of the same analyte -> one canonical marker.
_LAB_CANON = {
    "d dimer": "d-dimer",
    "rheumatoid factor": "rf",
}
_LAB_CORE = {
    "ana screen", "ana titer", "ana pattern", "anti-dsdna", "ena panel",
    "rf", "lupus band test",
    "c3", "c4", "igg", "iga", "igm",
    "crp", "esr", "d-dimer", "creatine kinase",
}
_LAB_ABNORMAL_ONLY = {
    "hdl", "ldl", "total cholesterol", "non-hdl cholesterol", "triglycerides",
    "lymphocytes", "leukocytes", "wbc", "neutrophils", "absolute eosinophils",
}
_LAB_ABNORMAL_FLAGS = {"high", "low", "critical", "abnormal"}
_LAB_ABNORMAL_QUAL = ("positive", "reactive", "detected", "abnormal")


def _lab_marker(lab):
    raw = (lab.get("test_name") or "").strip().lower()
    return _LAB_CANON.get(raw, raw)


def _lab_is_abnormal(lab):
    if (lab.get("flag") or "").strip().lower() in _LAB_ABNORMAL_FLAGS:
        return True
    q = (lab.get("qualitative_result") or "").strip().lower()
    return any(t in q for t in _LAB_ABNORMAL_QUAL)


def select_report_labs(all_labs):
    """Curated markers for the report — only what argues the case.

    A marker is surfaced only if it has been abnormal/positive at least once
    (a marker that's always been normal — IgG, CK, a normal CRP — is noise on a
    clinical handout). Exceptions kept for context: the ANA panel travels
    together (titer + pattern shown whenever the screen is positive), and
    complement is shown as a C3/C4 pair. Per surfaced marker, show the most
    recent value AND the most abnormal on record (if a different draw), so
    seroconversion history (RF/ANA once positive, now negative) is preserved.
    Spelling variants are collapsed. Returns one list, most recent first.
    """
    by_marker = {}
    for lab in all_labs:
        name = _lab_marker(lab)
        if name in _LAB_CORE or name in _LAB_ABNORMAL_ONLY:
            by_marker.setdefault(name, []).append(lab)

    # Markers worth surfacing: anything ever abnormal/positive...
    keep = {m for m, labs in by_marker.items()
            if any(_lab_is_abnormal(l) for l in labs)}
    # ...plus ANA-panel cohesion and complement pairing for context.
    if "ana screen" in keep:
        keep |= {"ana titer", "ana pattern"}
    if keep & {"c3", "c4"}:
        keep |= {"c3", "c4"}

    picked = []
    for name in keep:
        labs = by_marker.get(name) or []
        if name in _LAB_ABNORMAL_ONLY:          # lipids/WBC: only abnormal draws
            labs = [l for l in labs if _lab_is_abnormal(l)]
        if not labs:
            continue
        labs.sort(key=lambda l: l.get("date") or "")
        most_recent = labs[-1]
        chosen = [most_recent]
        abnormals = [l for l in labs if _lab_is_abnormal(l)]
        if abnormals and abnormals[-1] is not most_recent:
            chosen.append(abnormals[-1])        # most recent abnormal draw
        picked.extend(chosen)

    picked.sort(key=lambda l: l.get("date") or "", reverse=True)
    return picked


# Symptom categories for the report's frequency table (label order = display order)
_SYMPTOM_FREQ_KEYS = [
    ('neurological',   'Neurological'),
    ('cognitive',      'Cognitive'),
    ('musculature',    'Musculature'),
    ('migraine',       'Migraine'),
    ('pulmonary',      'Pulmonary'),
    ('dermatological', 'Dermatological'),
    ('rheumatic',      'Rheumatic'),
    ('gastro',         'Gastrointestinal'),
    ('mucosal',        'Mucosal'),
]


def symptom_frequency(observations):
    """Days each symptom category was flagged (categories present at least
    once), sorted by count descending. Shared by /report and the portal."""
    n_obs = len(observations)
    return sorted(
        [
            {'name': label, 'count': count,
             'percent': round(count / n_obs * 100) if n_obs else 0}
            for key, label in _SYMPTOM_FREQ_KEYS
            if (count := sum(1 for o in observations if o.get(key)))
        ],
        key=lambda x: x['count'], reverse=True
    )


def _serology_tags(key_labs):
    """Headline serology strings for the synopsis strip — the markers that
    argue the case (lupus band, ANA, dsDNA, complements)."""
    serology, seen = [], set()
    for lab in key_labs:
        nm = (lab.get("test_name") or "").lower()
        flg = (lab.get("flag") or "").lower()
        ql = (lab.get("qualitative_result") or "").lower()
        tag = None
        if "lupus band" in nm and "positive" in ql:
            tag = "lupus band +"
        elif nm.startswith("ana screen") and "positive" in ql:
            tag = "ANA +"
        elif "dsdna" in nm and flg in ("high", "abnormal", "critical"):
            tag = "anti-dsDNA ↑"
        elif nm == "c4" and flg == "low":
            tag = "C4 low"
        elif nm == "c3" and flg == "low":
            tag = "C3 low"
        if tag and tag not in seen:
            seen.add(tag)
            serology.append(tag)
    return serology


def _burden_series(all_obs_sorted, start_date, end_date, loc_key, user_id):
    """Per-day disease-burden score attribution for the window (same model as
    /model). Full history feeds the multi-day lookback; only the window gets
    scored. Shared by /report and the portal overview."""
    _inject_cycle_phase(all_obs_sorted)
    by_date = {o["date"]: o for o in all_obs_sorted}
    window = [o for o in all_obs_sorted if start_date <= o["date"] <= end_date]
    _inject_scoring_context(window, by_date, loc_key)
    series = []
    for o in window:
        comp = _score_components(o, user_id=user_id)
        series.append({
            "date": o["date"], "total": comp["total"],
            "uv": comp["uv"], "exertion": comp["exertion"],
            "temperature": comp["temperature"], "symptoms": comp["symptoms"],
            "pain_fatigue": comp["pain_fatigue"], "burden_delta": comp["burden_delta"],
            "rmssd": comp["rmssd"], "resp_rate": comp["resp_rate"],
            "flare": o.get("flare_occurred") == 1, "severity": o.get("flare_severity"),
        })
    return series


@app.route("/report")
def clinical_report():
    """Standalone clinical report page with auto-generated findings."""
    # Date range - default last 90 days
    end_date = request.args.get("end", date.today().isoformat())
    start_date = request.args.get(
        "start",
        (date.today() - timedelta(days=90)).isoformat()
    )
    
    # Fetch data for period
    observations = [o for o in db.get_all_daily_observations(uid())
                    if start_date <= o["date"] <= end_date]
    
    uv_data = db.get_uv_data_range(get_location_key(), start_date, end_date) if observations else []
    
    # Active medications
    all_meds = db.get_all_medications(uid())
    today_str = date.today().isoformat()
    active_meds = [m for m in all_meds
                   if m["start_date"] <= today_str and
                      (m.get("end_date") is None or m["end_date"] >= today_str)]

    # Disease-modifying therapy for the page-1 summary (HCQ, MMF, active systemic
    # steroids). Everything else — supplements, symptomatic, topical — drops to
    # the appendix med list. Topical/ENT steroids are excluded by name.
    _SYSTEMIC_STEROIDS = ("prednisone", "prednisolone", "methylprednisolone",
                          "methylprednisone", "dexamethasone")

    def _is_dmard(m):
        if m.get("is_primary_intervention"):
            return True
        name = (m.get("drug_name") or "").lower()
        return any(s in name for s in _SYSTEMIC_STEROIDS)

    dmard_meds = [m for m in active_meds if _is_dmard(m)]
    
    # Key diagnostic markers — curated, ALL-TIME (not window-scoped): the labs
    # that prove the case, plus any out-of-range lipids/WBC. See select_report_labs().
    all_labs = db.get_lab_results(uid())
    key_labs = select_report_labs(all_labs)
    
    # Clinical events in period
    all_events = db.get_clinical_events(uid())
    events = [e for e in all_events
              if start_date <= e["date"] <= end_date]
    events.sort(key=lambda x: x["date"], reverse=True)
    
    # Mean pain/fatigue for period
    pain_vals = [o.get("pain_scale") for o in observations
                 if o.get("pain_scale") is not None]
    fatigue_vals = [o.get("fatigue_scale") for o in observations
                    if o.get("fatigue_scale") is not None]
    
    mean_pain = round(sum(pain_vals) / len(pain_vals), 1) if pain_vals else None
    mean_fatigue = round(sum(fatigue_vals) / len(fatigue_vals), 1) if fatigue_vals else None

    # Flare summary
    flare_days = [o for o in observations if o.get('flare_occurred') == 1]
    flare_count = len(flare_days)
    flare_dates = sorted(o['date'] for o in flare_days)
    recent_flare = flare_dates[-1] if flare_dates else None

    # Symptom frequency (only categories present at least once)
    n_obs = len(observations)
    symptom_freq = symptom_frequency(observations)

    # ANA — all-time positive results only (negatives excluded; ANA fluctuates in early disease)
    all_ana = db.get_ana_results(uid()) if hasattr(db, 'get_ana_results') else []
    positive_ana = sorted(
        [a for a in all_ana
         if (a.get('screen_result') or '').lower().strip()
            not in ('negative', 'neg', 'nonreactive', '')],
        key=lambda a: a['date']
    )

    # Full tracking period
    all_obs = db.get_all_daily_observations(uid())
    tracking_start = all_obs[0]["date"] if all_obs else None
    tracking_end   = all_obs[-1]["date"] if all_obs else None
    
    # Primary intervention for report context
    prefs = get_user_prefs()
    intervention_name = prefs.get("primary_intervention_name") or (CONFIG.get("primary_intervention") or {}).get("name")
    intervention_date = prefs.get("primary_intervention_date") or (CONFIG.get("primary_intervention") or {}).get("start_date")

    # --- Disease-burden score attribution for the window (same model as /model).
    # Full history feeds the multi-day lookback; only the window gets scored. ---
    scored = sorted(all_obs, key=lambda x: x["date"])
    burden_threshold = get_current_weights(uid()).get("flare_threshold", 8.0)
    burden_data = _burden_series(scored, start_date, end_date,
                                 get_location_key(), uid())

    # --- Headline synopsis (clinicians scan highlights first) ---
    period_days = max((date.fromisoformat(end_date) - date.fromisoformat(start_date)).days, 1)
    serology = _serology_tags(key_labs)
    synopsis = {
        "flares_per_month": round(flare_count / period_days * 30, 1),
        "flare_count": flare_count,
        "period_days": period_days,
        "dmards": [m["drug_name"] for m in dmard_meds],
        "serology": serology,
        "mean_pain": mean_pain,
        "mean_fatigue": mean_fatigue,
    }

    return render_template(
        "report.html",
        start_date=start_date,
        end_date=end_date,
        tracking_start=tracking_start,
        tracking_end=tracking_end,
        patient_name=prefs.get("patient_name") or CONFIG.get("patient_name", ""),
        patient_dob=prefs.get("patient_dob") or CONFIG.get("patient_dob", ""),
        primary_intervention_name=intervention_name,
        primary_intervention_date=intervention_date,
        observations=observations,
        active_meds=active_meds,
        dmard_meds=dmard_meds,
        key_labs=key_labs,
        events=events,
        mean_pain=mean_pain,
        mean_fatigue=mean_fatigue,
        flare_count=flare_count,
        flare_dates=flare_dates,
        recent_flare=recent_flare,
        symptom_freq=symptom_freq,
        n_obs=n_obs,
        positive_ana=positive_ana,
        burden=burden_data,
        burden_threshold=burden_threshold,
        synopsis=synopsis,
        today=date.today().strftime("%B %d, %Y"),
    )

# ============================================================
# Health-sync API (iOS Shortcut / programmatic ingest)
# ============================================================

_HEALTH_SYNC_FIELDS = {"steps", "hrv", "hrv_rmssd", "resting_heart_rate", "basal_temp_delta", "sun_exposure_min", "spo2", "respiratory_rate"}

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

    if not fields_updated:
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

    return jsonify({"ok": True, "date": obs_date, "fields_updated": fields_updated})


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

# ============================================================
# DELETE ALL DATA
# ============================================================

@app.route("/delete/all-data", methods=["POST"])
def delete_all_data():
    """
    NUCLEAR OPTION: Delete ALL tracking data.
    This is irreversible and should only be called after multiple confirmations.
    """
    try:
        # Close any open connections
        db.close_all_connections()  
        
        # Delete the SQLite database file
        db_path = Path("biotracking.db")
        if db_path.exists():
            db_path.unlink()
        
        # Recreate empty database with schema
        from setup import create_database
        create_database()
        
        return jsonify({"success": True, "message": "All data deleted"}), 200
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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
# Mobile Quick Log & Status
# ============================================================

@app.route("/mobile/log", methods=["GET", "POST"])
@login_required
def mobile_log():
    """Ultra-minimal daily entry for mobile quick logging."""
    today = date.today().isoformat()
    existing = db.get_daily_observations(uid(), today)

    if request.method == "POST":
        form = request.form

        def get_bool(key):
            return 1 if form.get(key) == "on" else 0

        def get_float(key):
            val = form.get(key, "").strip()
            try:
                return float(val) if val else None
            except ValueError:
                return None

        # Only include fields present on this form — merge, don't overwrite
        data = {"date": today}

        # Biometrics — only include if field was submitted with a value
        for field in ("hours_slept", "hrv", "hrv_rmssd", "basal_temp_delta",
                      "steps", "sun_exposure_min"):
            val = get_float(field)
            if val is not None:
                data[field] = val

        # UV protection
        uv_prot = form.get("uv_protection_level")
        if uv_prot:
            data["uv_protection_level"] = uv_prot

        # Symptoms — always include (unchecked = 0, checked = 1)
        for sym in ("neurological", "cognitive", "musculature", "migraine",
                    "pulmonary", "dermatological", "rheumatic", "mucosal", "gastro"):
            data[sym] = get_bool(sym)

        # Pain + fatigue
        for field in ("pain_scale", "fatigue_scale"):
            val = get_float(field)
            if val is not None:
                data[field] = val

        # Flare
        data["flare_occurred"] = get_bool("flare_occurred")
        if data["flare_occurred"]:
            data["flare_severity"] = _clean_flare_severity(form.get("flare_severity"))

        db.upsert_daily_observations(uid(), data)
        return redirect(url_for("mobile_status"))

    return render_template("mobile_log.html", entry_date=today, existing=existing)


@app.route("/mobile/status")
@login_required
def mobile_status():
    """Mobile home screen — current risk at a glance."""
    all_obs = db.get_all_daily_observations(uid())
    if not all_obs or len(all_obs) < 7:
        return render_template("mobile_status.html", has_data=False)

    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)

    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, get_location_key(), n=14)

    scores = [calculate_flare_prime_score(obs) for obs in all_obs[:14]]
    today_score = scores[0]

    _fw = get_current_weights(uid())
    threshold = _fw.get('flare_threshold', 8.0)
    risk_info = get_risk_level(today_score, threshold)
    factors = get_contributing_factors(all_obs[0])
    score_delta = round(today_score - scores[1], 1) if len(scores) >= 2 else None

    # 14-day risk strip data
    risk_strip = []
    for i, obs in enumerate(all_obs[:14]):
        s = scores[i]
        if s >= threshold:
            color = '#c94040'
        elif s >= threshold * 0.65:
            color = '#d4a054'
        elif s >= threshold * 0.4:
            color = '#d4b84a'
        else:
            color = '#4a9e6e'
        risk_strip.append({
            'date': obs['date'],
            'score': s,
            'color': color,
            'flare': obs.get('flare_occurred') == 1,
        })

    return render_template(
        "mobile_status.html",
        has_data=True,
        today_score=round(today_score, 1),
        max_score=25,
        risk_level=risk_info['level'],
        risk_color=risk_info['color'],
        risk_description=risk_info['description'],
        predicted_flare=today_score >= threshold,
        score_delta=score_delta,
        factors=factors,
        risk_strip=list(reversed(risk_strip)),
        threshold=threshold,
    )


# ============================================================
# Help
# ============================================================

@app.route("/help")
def help_page():
    """Searchable help page."""
    return render_template("help.html")


@app.route("/readme")
@login_required
def readme_page():
    """Render the project README as a styled page (no nav link)."""
    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
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
    model_path = os.path.join(os.path.dirname(__file__), "MODEL.md")
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
    ra_path = os.path.join(os.path.dirname(__file__), "REMOTE_ACCESS.md")
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
    
