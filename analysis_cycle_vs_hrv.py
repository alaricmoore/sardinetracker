#!/usr/bin/env python3
"""
analysis_cycle_vs_hrv.py

One-off analysis: are luteal-phase and RMSSD-deviation independent flare
predictors, or is one downstream of the other?

Reproduces the three configurations Alaric ran through the UI's forecast-
accuracy view, then answers:

  Q1. On days where RMSSD deviation ≤ -25% (the high-weight rule fires),
      what fraction are luteal vs other cycle phases?
  Q2. Of the minor flares that flipped miss → catch when HRV weights were
      turned on, how many are outside luteal phase? (cleanest evidence of
      independent HRV signal)
  Q3. Of the majors caught by luteal alone, how many also had RMSSD firing?

Usage:
    python analysis_cycle_vs_hrv.py          # 60-obs window (matches UI default)
    python analysis_cycle_vs_hrv.py --days 90
"""

import argparse
import sqlite3
import sys
from collections import Counter

# Patch get_user_prefs BEFORE importing anything that uses it, so the
# cycle-phase + scoring helpers work without a Flask request context.
import app as _app_module
import db as _db_module

USER_ID = 1  # single-user instance

_user_prefs_cache = _db_module.get_user_preferences(USER_ID) or {}


def _patched_get_user_prefs():
    return _user_prefs_cache


def _patched_current_user_stub():
    class _Stub:
        is_authenticated = True
        id = USER_ID
    return _Stub()


# The scoring helpers live in flaremodel and the request helpers in appcore,
# and each module looks these names up in its own namespace, so every copy
# needs the patch - patching app alone leaves _inject_cycle_phase reading the
# real current_user, which is empty outside a request.
import appcore as _appcore_module
import flaremodel as _flaremodel_module
for _m in (_app_module, _appcore_module, _flaremodel_module):
    _m.get_user_prefs = _patched_get_user_prefs
    _m.current_user = _patched_current_user_stub()

from app import (  # noqa: E402
    _inject_cycle_phase,
    _inject_scoring_context,
    calculate_flare_prime_score,
    DEFAULT_WEIGHTS,
)

# Weights matching Alaric's live UI configuration (the three runs she
# compared all share these symptom/category settings; only the three HRV
# weights and cycle_phase differ between runs).
RUN1_WEIGHTS = DEFAULT_WEIGHTS.copy()
RUN1_WEIGHTS.update({
    'flare_threshold': 9.5,
    'uv_weight': 1.75,
    'exertion_weight': 1.0,
    'temperature_weight': 0.75,
    'pain_fatigue_weight': 1.25,
    'symptom_burden_weight': 1.0,
    'rmssd_deviation_weight': 1.25,
    'rmssd_instability_weight': 0.75,
    'resp_rate_deviation_weight': 0.5,
    'neurological': 0.75,
    'cognitive': 1.5,
    'musculature': 1.5,
    'migraine': 1.25,
    'pulmonary': 1.25,
    'dermatological': 1.5,
    'mucosal': 0.25,
    'rheumatic': 0.5,
    'cycle_phase': 1.5,
})

# Run 2: HRV weights zeroed (cycle still on). This is the config against
# which the "3 minors that flipped miss→catch with HRV" are defined.
RUN2_WEIGHTS_NO_HRV = RUN1_WEIGHTS.copy()
RUN2_WEIGHTS_NO_HRV.update({
    'rmssd_deviation_weight': 0.0,
    'rmssd_instability_weight': 0.0,
    'resp_rate_deviation_weight': 0.0,
})

THRESHOLD = RUN1_WEIGHTS['flare_threshold']
RMSSD_HIGH_FIRE = -25.0  # the "deviation ≤ -25%" rule in app.py:534


def _loc_key():
    lat = _user_prefs_cache.get('location_lat')
    lon = _user_prefs_cache.get('location_lon')
    if lat and lon:
        return _db_module.make_location_key(float(lat), float(lon))
    return 'default'


def _prepare_window(days: int):
    all_obs = _db_module.get_all_daily_observations(USER_ID)
    all_obs.sort(key=lambda x: x['date'], reverse=True)
    _inject_cycle_phase(all_obs)
    obs_by_date = {o['date']: o for o in all_obs}
    _inject_scoring_context(all_obs, obs_by_date, _loc_key())
    return all_obs[:days]


def _severity_of(obs):
    """UI's effective severity — lumps er_visit with major. Returns one of
    'major', 'minor', None."""
    if not obs.get('flare_occurred'):
        return None
    sev = obs.get('flare_severity')
    if sev in ('major', 'er_visit'):
        return 'major'
    if sev == 'minor':
        return 'minor'
    return 'unspec'


def _phase(obs):
    return obs.get('cycle_phase_name') or '(none)'


def _fmt_dev(dev):
    return f"{dev:+.1f}%" if dev is not None else "  n/a  "


def main(days: int):
    window = _prepare_window(days)

    # Score each obs under both weight configurations.
    rows = []
    for obs in window:
        s1 = calculate_flare_prime_score(obs, weights_override=RUN1_WEIGHTS)
        s2 = calculate_flare_prime_score(obs, weights_override=RUN2_WEIGHTS_NO_HRV)
        rows.append({
            'date':          obs['date'],
            'severity':      _severity_of(obs),
            'severity_raw':  obs.get('flare_severity'),  # preserves er_visit distinction
            'phase':         _phase(obs),
            'rmssd_dev':     obs.get('_rmssd_deviation'),
            'score_full':    s1,
            'score_nohrv':   s2,
            'caught_full':   s1 >= THRESHOLD,
            'caught_nohrv':  s2 >= THRESHOLD,
            'rmssd_firing':  (obs.get('_rmssd_deviation') or 0) <= RMSSD_HIGH_FIRE,
            'pain':          obs.get('pain_scale'),
            'fatigue':       obs.get('fatigue_scale'),
            'notes':         (obs.get('notes') or '').strip()[:120],
        })

    # ------------------------------------------------------------------
    print("=" * 72)
    print(f"LUTEAL vs RMSSD INDEPENDENCE ANALYSIS  —  window: {len(window)} obs")
    print(f"Threshold: {THRESHOLD}    RMSSD-high rule: deviation ≤ {RMSSD_HIGH_FIRE}%")
    print("=" * 72)

    # Sanity check against her UI-reported numbers.
    total_major = sum(1 for r in rows if r['severity'] == 'major')
    total_minor = sum(1 for r in rows if r['severity'] == 'minor')
    total_unspec = sum(1 for r in rows if r['severity'] == 'unspec')
    caught_major_full = sum(1 for r in rows if r['severity'] == 'major' and r['caught_full'])
    caught_minor_full = sum(1 for r in rows if r['severity'] == 'minor' and r['caught_full'])
    caught_major_nohrv = sum(1 for r in rows if r['severity'] == 'major' and r['caught_nohrv'])
    caught_minor_nohrv = sum(1 for r in rows if r['severity'] == 'minor' and r['caught_nohrv'])
    print()
    print(f"  Flares in window — major: {total_major}  minor: {total_minor}  "
          f"unspec: {total_unspec}")
    print(f"  Run 1 (HRV+cycle)  major caught: {caught_major_full}/{total_major}  "
          f"minor caught: {caught_minor_full}/{total_minor}")
    print(f"  Run 2 (cycle only) major caught: {caught_major_nohrv}/{total_major}  "
          f"minor caught: {caught_minor_nohrv}/{total_minor}")
    print("  (Compare to UI: run 1 = 7 major / 15 minor ; run 2 = 7 major / 12 minor)")
    print()

    # ------------------------------------------------------------------
    # Q1 — On days where RMSSD rule fires, cycle-phase distribution.
    print("─" * 72)
    print("Q1: RMSSD ≤ -25% firing days — cycle phase distribution")
    print("─" * 72)
    rmssd_firing = [r for r in rows if r['rmssd_firing']]
    phase_ct = Counter(r['phase'] for r in rmssd_firing)
    total_days = len(rows)
    n_fire = len(rmssd_firing)
    n_luteal_all = sum(1 for r in rows if r['phase'] == 'luteal')
    baseline_luteal_pct = 100 * n_luteal_all / total_days if total_days else 0
    print(f"  Total RMSSD-firing days: {n_fire} of {total_days} in window")
    for phase, ct in sorted(phase_ct.items(), key=lambda x: -x[1]):
        pct = 100 * ct / n_fire if n_fire else 0
        bar = "█" * int(pct / 3)
        print(f"    {phase:<12} {ct:3d}  ({pct:5.1f}%)  {bar}")
    print(f"  For reference, baseline luteal share of all days: {baseline_luteal_pct:.1f}%")
    print()

    # ------------------------------------------------------------------
    # Q2 — Minors that flipped miss → catch when HRV was added.
    print("─" * 72)
    print("Q2: Minor flares that flipped miss → catch with HRV (run 2 → run 1)")
    print("─" * 72)
    flipped_minor = [r for r in rows
                     if r['severity'] == 'minor'
                     and r['caught_full']
                     and not r['caught_nohrv']]
    outside_luteal = [r for r in flipped_minor if r['phase'] != 'luteal']
    print(f"  Total flipped minors: {len(flipped_minor)}")
    if flipped_minor:
        print(f"  {'date':<12} {'phase':<12} {'RMSSD':<10} "
              f"{'score (no HRV)':<17} {'score (full)':<14}")
        for r in sorted(flipped_minor, key=lambda x: x['date']):
            print(f"    {r['date']:<10} {r['phase']:<12} {_fmt_dev(r['rmssd_dev']):<10} "
                  f"{r['score_nohrv']:>8.2f}         {r['score_full']:>8.2f}")
    print(f"  Flipped minors OUTSIDE luteal: {len(outside_luteal)} of "
          f"{len(flipped_minor)}")
    if len(outside_luteal) == 0:
        print("  → HRV signal appears to ride with luteal (no independent catches).")
    elif len(outside_luteal) == len(flipped_minor):
        print("  → HRV signal fully independent of luteal on these catches.")
    else:
        print("  → HRV partially independent — some non-luteal catches suggest "
              "autonomic signal distinct from hormonal window.")
    print()

    # ------------------------------------------------------------------
    # Q3 — Majors caught by luteal alone: how many had RMSSD firing too?
    print("─" * 72)
    print("Q3: Majors caught by cycle-only config — RMSSD firing co-occurrence")
    print("─" * 72)
    majors_caught_nohrv = [r for r in rows
                           if r['severity'] == 'major' and r['caught_nohrv']]
    with_rmssd = [r for r in majors_caught_nohrv if r['rmssd_firing']]
    print(f"  Majors caught by cycle-only run: {len(majors_caught_nohrv)}")
    if majors_caught_nohrv:
        print(f"  {'date':<12} {'phase':<12} {'RMSSD':<10} {'firing?':<9} "
              f"{'score (no HRV)':<15}")
        for r in sorted(majors_caught_nohrv, key=lambda x: x['date']):
            mark = "YES" if r['rmssd_firing'] else "no"
            print(f"    {r['date']:<10} {r['phase']:<12} "
                  f"{_fmt_dev(r['rmssd_dev']):<10} {mark:<9} {r['score_nohrv']:>8.2f}")
    n_major = len(majors_caught_nohrv)
    n_both = len(with_rmssd)
    if n_major:
        pct = 100 * n_both / n_major
        print(f"  RMSSD also firing on {n_both}/{n_major} majors  ({pct:.0f}%)")
        if pct >= 70:
            print("  → RMSSD largely co-fires with cycle on majors → features "
                  "are correlated; HRV weight is double-counting on major recall.")
        elif pct <= 30:
            print("  → RMSSD rarely co-fires on majors → features independent; "
                  "HRV catches separate signal (even if it didn't flip any majors).")
        else:
            print("  → Mixed co-fire on majors; partial overlap.")
    print()

    # ------------------------------------------------------------------
    # Missed majors — ones the full config (HRV+cycle) failed to catch.
    # These are the days worth eyeballing to understand what the model
    # is still blind to.
    print("─" * 72)
    print("MISSED MAJORS (not caught by run 1 — full HRV+cycle config)")
    print("─" * 72)
    missed_majors = [r for r in rows if r['severity'] == 'major' and not r['caught_full']]
    if not missed_majors:
        print("  None in this window.")
    else:
        print(f"  {'date':<12} {'sev':<9} {'phase':<12} {'RMSSD':<10} "
              f"{'score':<7} {'gap':<7} {'pain':<5} {'fat':<5}")
        for r in sorted(missed_majors, key=lambda x: x['date']):
            gap = THRESHOLD - r['score_full']
            pain = r['pain'] if r['pain'] is not None else '-'
            fat = r['fatigue'] if r['fatigue'] is not None else '-'
            print(f"    {r['date']:<10} {str(r['severity_raw'] or '-'):<9} "
                  f"{r['phase']:<12} {_fmt_dev(r['rmssd_dev']):<10} "
                  f"{r['score_full']:>5.2f}  {gap:>+5.2f}  {str(pain):<5} {str(fat):<5}")
            if r['notes']:
                print(f"      note: {r['notes']}")
    print()

    # ------------------------------------------------------------------
    # Summary verdict
    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    luteal_pct_among_rmssd = (100 * phase_ct.get('luteal', 0) / n_fire) if n_fire else 0
    print(f"  Q1  RMSSD-firing days that are luteal: {luteal_pct_among_rmssd:.1f}%")
    print(f"      (baseline luteal share of all days: {baseline_luteal_pct:.1f}%)")
    print(f"  Q2  Minors HRV catches outside luteal: "
          f"{len(outside_luteal)} of {len(flipped_minor)}")
    if n_major:
        print(f"  Q3  Majors where RMSSD co-fires: {n_both}/{n_major}")
    else:
        print(f"  Q3  No majors in window caught by cycle-only run.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=60,
                        help='Size of reverse-chronological obs window (default 60)')
    args = parser.parse_args()
    main(args.days)
