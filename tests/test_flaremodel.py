"""
Tests for flaremodel.py, the shared scoring layer.

calculate_flare_prime_score is the number forecasts and alerts already use, so
these tests pin its current behaviour before the score breakdown and the
contributing-factors list are brought into line with it.

Weights are adjustable and vary between users and over time, so no test here
relies on stored weights: each one either passes weights explicitly or runs
with the fallback file pointed at nothing, which yields DEFAULT_WEIGHTS.
Expected values are worked out by hand from the code's thresholds.
"""

import json
from datetime import date, timedelta

import pytest

import flaremodel as fm

DAY0 = date(2026, 1, 1)


def iso(offset: int) -> str:
    return (DAY0 + timedelta(days=offset)).isoformat()


@pytest.fixture(autouse=True)
def isolated_model(monkeypatch, tmp_path):
    """No logged-in user and no stored weights."""
    class Anonymous:
        is_authenticated = False
    monkeypatch.setattr(fm, "current_user", Anonymous())
    monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(tmp_path / "no-custom-weights.json"))


def score(obs=None, **weights):
    """Prime score with DEFAULT_WEIGHTS, plus any weights given as keywords."""
    return fm.calculate_flare_prime_score(obs or {}, weights_override=weights or None)


# ------------------------------------------------------------------
# Weights
# ------------------------------------------------------------------

class TestWeights:
    def test_no_user_and_no_file_gives_defaults(self):
        assert fm.get_current_weights() == fm.DEFAULT_WEIGHTS

    def test_returns_a_copy(self):
        fm.get_current_weights()["migraine"] = 99
        assert fm.DEFAULT_WEIGHTS["migraine"] == 1.0

    def test_fallback_file_is_merged_over_defaults(self, monkeypatch, tmp_path):
        path = tmp_path / "custom_weights.json"
        path.write_text(json.dumps({"migraine": 0.5}))
        monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(path))
        weights = fm.get_current_weights()
        assert weights["migraine"] == 0.5
        assert weights["neurological"] == fm.DEFAULT_WEIGHTS["neurological"]

    def test_unreadable_fallback_file_gives_defaults(self, monkeypatch, tmp_path, capsys):
        path = tmp_path / "custom_weights.json"
        path.write_text("{not json")
        monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(path))
        assert fm.get_current_weights() == fm.DEFAULT_WEIGHTS

    def test_save_and_reset_without_a_user_use_the_file(self, monkeypatch, tmp_path):
        path = tmp_path / "config" / "custom_weights.json"
        monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(path))
        fm.save_custom_weights({"migraine": 2.0})
        assert fm.get_current_weights()["migraine"] == 2.0
        fm.reset_to_default_weights()
        assert not path.exists()
        assert fm.get_current_weights() == fm.DEFAULT_WEIGHTS

    def test_override_starts_from_defaults_not_stored_weights(self, monkeypatch, tmp_path):
        path = tmp_path / "custom_weights.json"
        path.write_text(json.dumps({"musculature": 9.0}))
        monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(path))
        assert score({"musculature": 1}) == 9.0
        assert score({"musculature": 1}, migraine=3.0) == 1.5

    def test_score_with_weights_delegates(self):
        obs = {"migraine": 1}
        assert fm.calculate_flare_score_with_weights(obs, {"migraine": 3.0}) == 3.0


class TestUserWeights:
    """Weights saved per user, in a database of the test's own."""

    @pytest.fixture
    def make_user(self, fresh_db):
        import db
        return lambda name: db.create_user(name, name.title(), "not-a-real-password-hash")

    def test_saved_weights_merge_over_defaults(self, make_user):
        user = make_user("tester")
        fm.save_custom_weights({"migraine": 2.0}, user_id=user)
        weights = fm.get_current_weights(user)
        assert weights["migraine"] == 2.0
        assert weights["neurological"] == fm.DEFAULT_WEIGHTS["neurological"]

    def test_user_weights_win_over_the_fallback_file(self, make_user, monkeypatch, tmp_path):
        path = tmp_path / "custom_weights.json"
        path.write_text(json.dumps({"musculature": 9.0}))
        monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(path))
        user = make_user("tester")
        fm.save_custom_weights({"musculature": 2.0}, user_id=user)
        assert fm.get_current_weights(user)["musculature"] == 2.0

    def test_a_user_without_saved_weights_gets_the_fallback_file(self, make_user, monkeypatch, tmp_path):
        path = tmp_path / "custom_weights.json"
        path.write_text(json.dumps({"musculature": 2.5}))
        monkeypatch.setattr(fm, "CUSTOM_WEIGHTS_PATH", str(path))
        assert fm.get_current_weights(make_user("newcomer"))["musculature"] == 2.5

    def test_reset_clears_the_users_saved_weights(self, make_user):
        user = make_user("tester")
        fm.save_custom_weights({"migraine": 2.0}, user_id=user)
        fm.reset_to_default_weights(user_id=user)
        assert fm.get_current_weights(user) == fm.DEFAULT_WEIGHTS

    def test_users_keep_separate_weights(self, make_user):
        alvin, gryf = make_user("alvin"), make_user("gryf")
        fm.save_custom_weights({"migraine": 3.0}, user_id=alvin)
        assert fm.get_current_weights(alvin)["migraine"] == 3.0
        assert fm.get_current_weights(gryf)["migraine"] == fm.DEFAULT_WEIGHTS["migraine"]

    def test_corrupt_saved_weights_fall_back(self, make_user):
        import db
        user = make_user("tester")
        db.upsert_user_preferences(user, {"custom_weights": "{not json"})
        assert fm.get_current_weights(user) == fm.DEFAULT_WEIGHTS


# ------------------------------------------------------------------
# Flare prime score, category by category
# ------------------------------------------------------------------

class TestPrimeScore:
    def test_an_empty_day_scores_zero(self):
        assert score() == 0.0

    # UV: weighted UV of {"uv_noon": 10} is 6.0, and 6 ** 1.5 = 14.70
    NOON_10 = {"uv_noon": 10}

    @pytest.mark.parametrize("sun_min, protection, expected", [
        (60, None, 6.0),             # dose 881.8 -> 3 pts, x uv_weight 2
        (30, None, 2.5),             # dose 440.9 -> 1.25 pts
        (27, None, 0.0),             # dose 396.8, below 400
        (60, "spf_hat", 2.5),        # 881.8 x 0.5 = 440.9
        (60, "indoors_only", 0.0),   # 881.8 x 0.1 = 88.2
    ])
    def test_uv_dose(self, sun_min, protection, expected):
        obs = {"_uv_row": self.NOON_10, "sun_exposure_min": sun_min, "uv_protection_level": protection}
        assert score(obs, uv_weight=2.0) == expected

    @pytest.mark.parametrize("cumulative, expected", [(2500, 3.0), (1500, 1.5), (1499, 0.0)])
    def test_cumulative_uv_bonus(self, cumulative, expected):
        assert score({"_cumulative_uv_dose": cumulative}, uv_weight=2.0) == expected

    @pytest.mark.parametrize("steps, expected", [(16000, 2.0), (12000, 1.5), (11999, 0.0)])
    def test_exertion_without_a_personal_baseline_uses_steps_per_hour_slept(self, steps, expected):
        assert score({"steps": steps, "hours_slept": 8}) == expected

    @pytest.mark.parametrize("steps, hours, expected", [
        (18000, 8, 2.0),   # 1.8x baseline
        (14000, 8, 1.5),   # 1.4x
        (7000, 4, 1.5),    # 0.7x baseline, but half the sleep doubles it to 1.4
        (13000, 8, 0.0),   # 1.3x
    ])
    def test_exertion_against_a_personal_baseline(self, steps, hours, expected):
        obs = {"steps": steps, "hours_slept": hours, "_steps_baseline": 10000}
        assert score(obs) == expected

    @pytest.mark.parametrize("delta, expected", [(0.8, 3.0), (0.5, 2.0), (0.3, 1.0), (0.29, 0.0)])
    def test_basal_temperature(self, delta, expected):
        assert score({"basal_temp_delta": delta}) == expected

    def test_symptom_flags_use_the_symptom_weights(self):
        flags = {s: 1 for s in ("neurological", "cognitive", "musculature", "migraine",
                                "pulmonary", "dermatological", "mucosal")}
        assert score(flags) == 1.5 + 1.0 + 1.5 + 1.0 + 1.0 + 0.75 + 0.25
        assert score({"migraine": 1}, migraine=3.0) == 3.0

    @pytest.mark.parametrize("notes, expected", [
        ("left knee", 2.0), ("finger joints", 1.0), ("hand and knee", 2.0), ("", 0.5), (None, 0.5),
    ])
    def test_rheumatic_scores_by_joint_size(self, notes, expected):
        assert score({"rheumatic": 1, "rheumatic_notes": notes}) == expected

    @pytest.mark.parametrize("field", ["pain_scale", "fatigue_scale"])
    @pytest.mark.parametrize("value, expected", [(7, 3.5), (6, 2.5), (5, 1.5), (4, 0.5), (3, 0.0)])
    def test_pain_and_fatigue_ladders(self, field, value, expected):
        assert score({field: value}) == expected
        assert score({field: value}, pain_fatigue_weight=2.0) == expected * 2

    @pytest.mark.parametrize("mood, expected", [(4, 2.0), (5, 0.0), (None, 0.0)])
    def test_low_emotional_state(self, mood, expected):
        assert score({"emotional_state": mood}) == expected

    def test_cycle_phase_adds_its_weight(self):
        obs = {"cycle_in_high_risk_phase": True}
        assert score(obs, cycle_phase=2.0) == 2.0
        assert score(obs, cycle_phase=0.0) == 0.0

    @pytest.mark.parametrize("delta, expected", [(3.0, 3.0), (2.0, 2.0), (1.0, 1.0), (0.99, 0.0)])
    def test_symptom_burden_delta(self, delta, expected):
        assert score({"_symptom_burden_delta": delta}) == expected

    @pytest.mark.parametrize("field, value, expected", [
        ("_rmssd_deviation", -25, 3.0), ("_rmssd_deviation", -15, 1.5), ("_rmssd_deviation", -14.9, 0.0),
        ("_rmssd_instability", 50, 3.0), ("_rmssd_instability", 25, 1.5), ("_rmssd_instability", 24.9, 0.0),
        ("_resp_rate_deviation", 15, 3.0), ("_resp_rate_deviation", 10, 1.5), ("_resp_rate_deviation", 9.9, 0.0),
    ])
    def test_autonomic_signals(self, field, value, expected):
        weights = {"rmssd_deviation_weight": 2.0, "rmssd_instability_weight": 2.0,
                   "resp_rate_deviation_weight": 2.0}
        assert score({field: value}, **weights) == expected

    def test_categories_add_up(self):
        obs = {"_uv_row": {"uv_noon": 10}, "sun_exposure_min": 60,   # 3.0
               "migraine": 1,                                        # 1.0
               "pain_scale": 6,                                      # 2.5
               "emotional_state": 3,                                 # 2.0
               "_symptom_burden_delta": 2.5}                         # 2.0
        assert score(obs) == 10.5


# ------------------------------------------------------------------
# Symptom points and severity vocabulary
# ------------------------------------------------------------------

class TestSymptomPoints:
    @pytest.fixture(autouse=True)
    def vocab(self, monkeypatch):
        monkeypatch.setattr(fm, "severity_score", lambda notes: {"worst ever": 2.0}.get(notes))

    def test_unflagged_symptom_scores_nothing_even_with_notes(self):
        assert fm.symptom_points("migraine", {"migraine": 0, "migraine_notes": "worst ever"}, 1.0) == 0.0

    def test_severity_notes_replace_the_weight(self):
        assert fm.symptom_points("migraine", {"migraine": 1, "migraine_notes": "worst ever"}, 1.0) == 2.0

    def test_bland_or_missing_notes_fall_back_to_the_weight(self):
        assert fm.symptom_points("migraine", {"migraine": 1, "migraine_notes": "meh"}, 1.25) == 1.25
        assert fm.symptom_points("migraine", {"migraine": 1}, 1.25) == 1.25


# ------------------------------------------------------------------
# Risk level
# ------------------------------------------------------------------

@pytest.mark.parametrize("value, threshold, level", [
    (4.99, 8.0, "Low Risk"), (5.0, 8.0, "Moderate Risk"), (7.99, 8.0, "Moderate Risk"),
    (8.0, 8.0, "High Risk"), (11.99, 8.0, "High Risk"), (12.0, 8.0, "Critical Risk"),
    (9.99, 16.0, "Low Risk"), (10.0, 16.0, "Moderate Risk"), (24.0, 16.0, "Critical Risk"),
])
def test_risk_level_breakpoints_scale_with_threshold(value, threshold, level):
    assert fm.get_risk_level(value, threshold)["level"] == level


# ------------------------------------------------------------------
# Cumulative UV and scoring context
# ------------------------------------------------------------------

class TestCumulativeUV:
    def test_prior_four_days_decay_and_protection(self, monkeypatch):
        rows = {iso(-off): {"uv_noon": 10} for off in (1, 2, 4, 5)}
        monkeypatch.setattr(fm.db, "get_uv_data", lambda loc, d: rows.get(d))
        obs_by_date = {
            iso(-1): {"sun_exposure_min": 100},
            iso(-2): {"sun_exposure_min": 100, "uv_protection_level": "full_cover"},
            iso(-3): {"sun_exposure_min": 100},   # no UV row that day: skipped
            iso(-4): {"sun_exposure_min": 100},
            iso(-5): {"sun_exposure_min": 100},   # outside the 4-day window
        }
        one_day = 6.0 ** 1.5 * 100
        expected = one_day * (0.8 + 0.6 * 0.3 + 0.2)
        assert fm._compute_cumulative_uv(iso(0), obs_by_date, "loc") == pytest.approx(expected)

    def test_same_day_uv_is_not_included(self, monkeypatch):
        monkeypatch.setattr(fm.db, "get_uv_data", lambda loc, d: {"uv_noon": 10} if d == iso(0) else None)
        assert fm._compute_cumulative_uv(iso(0), {iso(0): {"sun_exposure_min": 100}}, "loc") == 0.0

    def test_inject_scoring_context_respects_n(self, monkeypatch):
        monkeypatch.setattr(fm.db, "get_uv_data", lambda loc, d: None)
        obs = [{"date": iso(0)}, {"date": iso(1)}]
        fm._inject_scoring_context(obs, {o["date"]: o for o in obs}, "loc", n=1)
        assert obs[0]["_cumulative_uv_dose"] == 0.0
        assert obs[0]["_symptom_burden_delta"] is None
        assert "_cumulative_uv_dose" not in obs[1]


# ------------------------------------------------------------------
# Period, ovulation and cycle phase
# ------------------------------------------------------------------

def flow_days(flows: dict) -> list:
    """Sorted observations from {day offset: period_flow}."""
    return [{"date": iso(off), "period_flow": f} for off, f in sorted(flows.items())]


class TestPeriodStarts:
    def test_first_real_flow_starts_a_period(self):
        assert fm._detect_period_starts(flow_days({0: "medium"})) == [iso(0)]

    def test_spotting_alone_never_starts_one(self):
        assert fm._detect_period_starts(flow_days({0: "spotting", 1: "spotting"})) == []

    @pytest.mark.parametrize("flows, start", [
        ({0: "spotting", 1: "light"}, 0),
        ({0: "spotting", 1: "spotting", 2: "heavy"}, 0),
        ({0: "spotting", 2: "medium"}, 2),   # a gap day stops the look-back
    ])
    def test_spotting_just_before_flow_moves_the_start_back(self, flows, start):
        assert fm._detect_period_starts(flow_days(flows)) == [iso(start)]

    def test_three_logged_days_without_flow_close_a_period(self):
        flows = {0: "medium", 1: "medium", 2: "medium", 3: None, 4: None, 5: None, 6: "medium"}
        assert fm._detect_period_starts(flow_days(flows)) == [iso(0), iso(6)]

    def test_next_cycle(self):
        flows = {**{d: "medium" for d in range(5)}, 28: "medium"}
        assert fm._detect_period_starts(flow_days(flows)) == [iso(0), iso(28)]

    @pytest.mark.xfail(strict=True, reason=(
        "Code and docstring disagree: the code closes a period once 3 calendar days "
        "have passed since the last flow day, so 2 blank days then flow starts a new "
        "period. Awaiting a decision on which behaviour is intended."))
    def test_two_days_without_flow_do_not_close_a_period(self):
        # Docstring: "closes only when 3 consecutive days have no flow logged".
        flows = {0: "medium", 1: "medium", 2: "medium", 3: None, 4: None, 5: "light"}
        assert fm._detect_period_starts(flow_days(flows)) == [iso(0)]

    @pytest.mark.xfail(strict=True, reason=(
        "Code and docstring disagree: the code measures the gap in calendar days, so "
        "unlogged days count as no flow. Awaiting a decision on which is intended."))
    def test_unlogged_days_count_as_unknown_not_as_no_flow(self):
        # Docstring: "missing days count as 'unknown', not 'no flow'".
        flows = {0: "medium", 1: "medium", 2: "medium", 6: "light"}
        assert fm._detect_period_starts(flow_days(flows)) == [iso(0)]


class TestOvulation:
    def bbt(self, values: dict) -> dict:
        return {iso(off): v for off, v in values.items()}

    def test_first_day_of_a_three_day_sustained_rise(self):
        readings = self.bbt({**{d: 0.0 for d in range(1, 6)}, 6: 0.3, 7: 0.3, 8: 0.3})
        assert fm._detect_ovulation_bbt(readings, DAY0, DAY0 + timedelta(days=28)) == DAY0 + timedelta(days=6)

    def test_rise_is_measured_from_the_first_five_readings(self):
        # Follicular average 0.0, so the threshold is 0.1 exactly.
        readings = self.bbt({**{d: 0.0 for d in range(1, 6)}, 6: 0.1, 7: 0.1, 8: 0.1})
        assert fm._detect_ovulation_bbt(readings, DAY0, DAY0 + timedelta(days=28)) == DAY0 + timedelta(days=6)

    def test_an_interrupted_rise_starts_over(self):
        readings = self.bbt({**{d: 0.0 for d in range(1, 6)},
                             6: 0.3, 7: 0.0, 8: 0.3, 9: 0.3, 10: 0.3})
        assert fm._detect_ovulation_bbt(readings, DAY0, DAY0 + timedelta(days=28)) == DAY0 + timedelta(days=8)

    def test_needs_eight_readings(self):
        readings = self.bbt({**{d: 0.0 for d in range(1, 6)}, 6: 0.3, 7: 0.3})
        assert fm._detect_ovulation_bbt(readings, DAY0, DAY0 + timedelta(days=28)) is None

    def test_cycle_end_is_exclusive(self):
        readings = self.bbt({**{d: 0.0 for d in range(1, 6)}, 6: 0.3, 7: 0.3, 8: 0.3})
        assert fm._detect_ovulation_bbt(readings, DAY0, DAY0 + timedelta(days=8)) is None


class TestCyclePhase:
    @pytest.fixture
    def tracking(self, monkeypatch):
        monkeypatch.setitem(fm.CONFIG, "track_cycle", True)

    def test_without_bbt_luteal_is_the_14_days_before_the_next_period(self, tracking):
        phases = fm._compute_phase_by_date_from_obs(flow_days({0: "medium", 28: "medium"}))
        assert iso(13) not in phases
        assert phases[iso(14)] == "luteal" and phases[iso(20)] == "luteal"
        assert phases[iso(21)] == "pms" and phases[iso(27)] == "pms"
        assert iso(28) not in phases

    def test_detected_ovulation_moves_the_luteal_start(self, tracking):
        obs = flow_days({0: "medium", 28: "medium"})
        obs += [{"date": iso(d), "basal_temp_delta": 0.0} for d in range(1, 6)]
        obs += [{"date": iso(d), "basal_temp_delta": 0.3} for d in (6, 7, 8)]
        phases = fm._compute_phase_by_date_from_obs(obs)
        assert iso(5) not in phases
        assert phases[iso(6)] == "luteal" and phases[iso(12)] == "luteal"
        assert phases[iso(13)] == "pms"

    def test_last_cycle_is_projected_from_recent_lengths_ignoring_long_gaps(self, tracking):
        # Lengths 100 and 28: the 100-day gap is excluded, so the open last
        # cycle is projected at 28 days and its luteal phase starts on day 142.
        phases = fm._compute_phase_by_date_from_obs(flow_days({0: "medium", 100: "medium", 128: "medium"}))
        assert iso(141) not in phases
        assert phases[iso(142)] == "luteal"

    def test_needs_two_periods(self, tracking):
        assert fm._compute_phase_by_date_from_obs(flow_days({0: "medium"})) == {}

    def test_off_when_cycle_tracking_is_off(self, monkeypatch):
        monkeypatch.setitem(fm.CONFIG, "track_cycle", False)
        assert fm._compute_phase_by_date_from_obs(flow_days({0: "medium", 28: "medium"})) == {}

    def test_inject_cycle_phase_annotates_every_day(self, tracking):
        obs = flow_days({0: "medium", 14: None, 28: "medium"})
        fm._inject_cycle_phase(obs)
        by_date = {o["date"]: o for o in obs}
        assert by_date[iso(14)]["cycle_in_high_risk_phase"] is True
        assert by_date[iso(14)]["cycle_phase_name"] == "luteal"
        assert by_date[iso(0)]["cycle_in_high_risk_phase"] is False
        assert by_date[iso(0)]["cycle_phase_name"] is None
