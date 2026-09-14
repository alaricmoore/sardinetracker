"""
Forecast pages: the forecast lab and its simulation, the forecast, its
history, accuracy self-grading, and pre-flare patterns.
"""

from scoring import UV_PROTECTION_MULTIPLIERS, weighted_uv
import json
import os
import statistics
from datetime import date, timedelta
from flask import render_template, request, redirect, url_for
from flask_login import login_required, current_user
import db

from appcore import CONFIG, app, get_location_key, get_user_prefs, uid
from flaremodel import CUSTOM_WEIGHTS_PATH, _inject_cycle_phase, _inject_scoring_context, _score_components, calculate_flare_prime_score, calculate_flare_score_with_weights, get_contributing_factors, get_current_weights, get_risk_level, reset_to_default_weights, save_custom_weights
from routes.dashboard import _compute_personal_lag_summary


    

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
