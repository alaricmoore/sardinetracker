"""
Tests for scoring.py, the pure scoring primitives.

Expected values are worked out by hand from the formulas and the windows
documented in each function, not copied from a run of the code. A test that
records whatever the code returns today would pass a bug as happily as a fix.
"""

from datetime import date, timedelta

import pytest

import scoring
from scoring import (
    UNCALIBRATED_RMSSD_BOUNDS,
    UV_PROTECTION_MULTIPLIERS,
    _compute_resp_rate_deviation,
    _compute_rmssd_deviation,
    _compute_rmssd_instability,
    _compute_symptom_burden_delta,
    _daily_symptom_count,
    _percentile,
    _resp_rate_deviation_detail,
    _rmssd_deviation_detail,
    _rmssd_instability_detail,
    _symptom_burden_detail,
    calibrate_rmssd_bounds,
    compute_rmssd,
    rmssd_is_implausible,
    weighted_uv,
)

TARGET = "2026-03-20"
NONE3 = (None, None, None)


def days(values: dict) -> dict:
    """Build obs_by_date from {days_before_TARGET: observation dict}."""
    t = date.fromisoformat(TARGET)
    return {(t - timedelta(days=off)).isoformat(): obs for off, obs in values.items()}


def symptoms(n: int) -> dict:
    """An observation with the first n symptom flags set."""
    return {key: 1 for key in scoring._SYMPTOM_KEYS[:n]}


# ------------------------------------------------------------------
# UV
# ------------------------------------------------------------------

class TestWeightedUV:
    def test_weights_noon_most(self):
        row = {"uv_morning": 2, "uv_noon": 8, "uv_evening": 4}
        assert weighted_uv(row) == pytest.approx(2 * 0.2 + 8 * 0.6 + 4 * 0.2)

    def test_missing_or_null_readings_count_as_zero(self):
        assert weighted_uv({"uv_noon": 5, "uv_morning": None}) == pytest.approx(3.0)

    def test_no_row_is_zero(self):
        assert weighted_uv(None) == 0.0
        assert weighted_uv({}) == 0.0

    def test_numeric_strings_are_accepted(self):
        assert weighted_uv({"uv_noon": "5"}) == pytest.approx(3.0)


def test_uv_protection_multipliers_are_pinned():
    # Changing these changes every computed UV risk score, and scores either
    # side of a change are not comparable. If this fails on purpose, update
    # MODEL.md and note the change in the commit message.
    assert UV_PROTECTION_MULTIPLIERS == {
        "none": 1.0, "spf_hat": 0.5, "full_cover": 0.3, "indoors_only": 0.1,
    }


# ------------------------------------------------------------------
# RMSSD from RR intervals
# ------------------------------------------------------------------

class TestComputeRMSSD:
    def test_known_value(self):
        # successive differences 10 and -20 -> mean square 250 -> sqrt 15.81
        assert compute_rmssd([800, 810, 790]) == 15.81

    def test_steady_rhythm_is_zero(self):
        assert compute_rmssd([800, 800, 800, 800]) == 0.0

    @pytest.mark.parametrize("rr", [[], [800]])
    def test_needs_two_intervals(self, rr):
        assert compute_rmssd(rr) is None


# ------------------------------------------------------------------
# Symptom counting and burden delta
# ------------------------------------------------------------------

class TestDailySymptomCount:
    def test_counts_only_symptom_flags(self):
        obs = {"neurological": 1, "migraine": 1, "gastro": 0, "pain_scale": 7}
        assert _daily_symptom_count(obs) == 2

    def test_missing_day_is_none_not_zero(self):
        # None means "no row", which the windows skip. Zero would drag an
        # average down as if a symptom-free day had been logged.
        assert _daily_symptom_count(None) is None

    def test_empty_dict_is_treated_as_missing(self):
        assert _daily_symptom_count({}) is None


class TestSymptomBurden:
    def test_recent_minus_baseline(self):
        obs = days({1: symptoms(3), 2: symptoms(3), 3: symptoms(3),
                    **{off: symptoms(1) for off in range(4, 18)}})
        assert _symptom_burden_detail(TARGET, obs) == (2.0, 3.0, 1.0)
        assert _compute_symptom_burden_delta(TARGET, obs) == 2.0

    def test_day_minus_3_is_recent_not_baseline(self):
        # The windows must not overlap at -3, or the start of a pre-flare ramp
        # inflates the baseline and hides the delta it should reveal.
        obs = days({3: symptoms(6), **{off: symptoms(0) | {"pain_scale": 1} for off in range(4, 18)}})
        delta, recent, baseline = _symptom_burden_detail(TARGET, obs)
        assert (recent, baseline) == (6.0, 0.0)
        assert delta == 6.0

    def test_target_day_and_day_minus_18_are_outside_both_windows(self):
        obs = days({0: symptoms(9), 18: symptoms(9), 1: symptoms(2),
                    **{off: symptoms(1) for off in range(4, 11)}})
        assert _symptom_burden_detail(TARGET, obs) == (1.0, 2.0, 1.0)

    def test_needs_seven_baseline_days(self):
        six = days({1: symptoms(2), **{off: symptoms(1) for off in range(4, 10)}})
        seven = days({1: symptoms(2), **{off: symptoms(1) for off in range(4, 11)}})
        assert _symptom_burden_detail(TARGET, six) == NONE3
        assert _symptom_burden_detail(TARGET, seven)[0] == 1.0

    def test_needs_a_recent_day(self):
        obs = days({off: symptoms(1) for off in range(4, 18)})
        assert _symptom_burden_detail(TARGET, obs) == NONE3


# ------------------------------------------------------------------
# RMSSD level deviation
# ------------------------------------------------------------------

class TestRMSSDDeviation:
    def test_percent_below_baseline_is_negative(self):
        obs = days({**{off: {"hrv_rmssd": 20} for off in range(1, 8)},
                    **{off: {"hrv_rmssd": 25} for off in range(8, 38)}})
        assert _rmssd_deviation_detail(TARGET, obs) == (-20.0, 20.0, 25.0)
        assert _compute_rmssd_deviation(TARGET, obs) == -20.0

    def test_recent_window_is_days_1_to_7(self):
        # A reading on day 8 belongs to the baseline, not the recent week.
        obs = days({**{off: {"hrv_rmssd": 20} for off in range(1, 8)},
                    8: {"hrv_rmssd": 40},
                    **{off: {"hrv_rmssd": 25} for off in range(9, 12)}})
        _, recent, baseline = _rmssd_deviation_detail(TARGET, obs)
        assert recent == 20.0
        assert baseline == pytest.approx((40 + 25 * 3) / 4)

    def test_nulls_are_skipped(self):
        obs = days({**{off: {"hrv_rmssd": 20} for off in range(1, 5)},
                    5: {"hrv_rmssd": None},
                    **{off: {"hrv_rmssd": 25} for off in range(8, 12)}})
        assert _rmssd_deviation_detail(TARGET, obs)[1] == 20.0

    def test_underpowered_windows_return_none(self):
        three_recent = days({**{off: {"hrv_rmssd": 20} for off in range(1, 4)},
                             **{off: {"hrv_rmssd": 25} for off in range(8, 38)}})
        assert _rmssd_deviation_detail(TARGET, three_recent) == NONE3

    def test_zero_baseline_returns_none_instead_of_dividing(self):
        obs = days({**{off: {"hrv_rmssd": 20} for off in range(1, 8)},
                    **{off: {"hrv_rmssd": 0} for off in range(8, 38)}})
        assert _rmssd_deviation_detail(TARGET, obs) == NONE3


# ------------------------------------------------------------------
# RMSSD instability
# ------------------------------------------------------------------

def _swinging_rmssd():
    """Recent days (1..6) swing by 10 ms a day; the baseline (6..36) by 5 ms.

    Day 6 is the hinge: it is the 'previous day' for the last recent delta and
    the 'current day' for the first baseline delta, and takes 30 in both runs.
    """
    values = {}
    for off in range(1, 37):
        if off <= 6:
            values[off] = 30 if off % 2 == 0 else 20
        else:
            values[off] = 30 if off % 2 == 0 else 25
    return days({off: {"hrv_rmssd": v} for off, v in values.items()})


class TestRMSSDInstability:
    def test_recent_swings_twice_baseline_is_plus_100(self):
        obs = _swinging_rmssd()
        assert _rmssd_instability_detail(TARGET, obs) == (100.0, 10.0, 5.0)
        assert _compute_rmssd_instability(TARGET, obs) == 100.0

    def test_needs_three_recent_deltas(self):
        # Days 1-3 give only two adjacent pairs.
        obs = days({1: {"hrv_rmssd": 20}, 2: {"hrv_rmssd": 30}, 3: {"hrv_rmssd": 20},
                    **{off: {"hrv_rmssd": 30 if off % 2 else 25} for off in range(6, 37)}})
        assert _rmssd_instability_detail(TARGET, obs) == NONE3

    def test_a_missing_day_drops_the_pair_on_both_sides(self):
        # Give each recent pair a different size so the mean shows which were used.
        #   pair:   (1,2) (2,3) (3,4) (4,5) (5,6)
        #   |d|:     10    20    10    30    30     -> mean 20
        obs = _swinging_rmssd()
        for off, v in {1: 10, 2: 20, 3: 40, 4: 30, 5: 60}.items():
            obs[(date.fromisoformat(TARGET) - timedelta(days=off)).isoformat()] = {"hrv_rmssd": v}
        assert _rmssd_instability_detail(TARGET, obs) == (300.0, 20.0, 5.0)

        # Without day 3, pairs (2,3) and (3,4) both go: 10, 30, 30 remain.
        # Dropping only one of them would leave a mean of 20 or 22.5 instead.
        del obs[(date.fromisoformat(TARGET) - timedelta(days=3)).isoformat()]
        _, recent, _ = _rmssd_instability_detail(TARGET, obs)
        assert recent == pytest.approx(70 / 3)

        # Without day 5 as well, only (1,2) is left, below the 3-delta minimum.
        del obs[(date.fromisoformat(TARGET) - timedelta(days=5)).isoformat()]
        assert _rmssd_instability_detail(TARGET, obs) == NONE3


# ------------------------------------------------------------------
# Respiratory rate deviation
# ------------------------------------------------------------------

class TestRespRateDeviation:
    def test_elevated_rate_is_positive(self):
        obs = days({**{off: {"respiratory_rate": 18} for off in range(1, 4)},
                    **{off: {"respiratory_rate": 15} for off in range(4, 18)}})
        assert _resp_rate_deviation_detail(TARGET, obs) == (20.0, 18.0, 15.0)
        assert _compute_resp_rate_deviation(TARGET, obs) == 20.0

    def test_minimums_are_two_recent_and_four_baseline(self):
        base = {off: {"respiratory_rate": 15} for off in range(4, 18)}
        one_recent = days({1: {"respiratory_rate": 18}, **base})
        three_baseline = days({1: {"respiratory_rate": 18}, 2: {"respiratory_rate": 18},
                               **{off: {"respiratory_rate": 15} for off in range(4, 7)}})
        assert _resp_rate_deviation_detail(TARGET, one_recent) == NONE3
        assert _resp_rate_deviation_detail(TARGET, three_baseline) == NONE3


# ------------------------------------------------------------------
# RMSSD artifact guard
# ------------------------------------------------------------------

class TestPercentile:
    def test_interpolates(self):
        # k = 4 * 0.95 = 3.8 -> 4 + (5 - 4) * 0.8
        assert _percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
        assert _percentile([1, 2, 3, 4, 5], 50) == 3

    def test_order_does_not_matter(self):
        assert _percentile([5, 1, 4, 2, 3], 95) == pytest.approx(4.8)

    def test_edges(self):
        assert _percentile([7], 95) == 7
        assert _percentile([1, 2, 3], 100) == 3
        with pytest.raises(ValueError):
            _percentile([], 50)


class TestCalibrateRMSSDBounds:
    def test_too_little_history_stays_uncalibrated(self):
        bounds = calibrate_rmssd_bounds([(10, 20)] * 89)
        assert bounds == {"ceiling_ms": 200.0, "ratio_max": 10.0, "calibrated": False, "n": 89}

    def test_does_not_mutate_the_shared_default(self):
        calibrate_rmssd_bounds([(10, 20)] * 5)
        assert UNCALIBRATED_RMSSD_BOUNDS["n"] == 0

    def test_unusable_rows_are_not_counted(self):
        junk = [(None, 5), (0, 5), (-3, 5), ("x", 5), (5,), None]
        assert calibrate_rmssd_bounds(junk)["n"] == 0

    def test_low_rmssd_user_gets_the_floors(self):
        # P95 10 * 2.25 = 22.5 -> floor 50. Ratio 0.5 * 2.5 = 1.25 -> floor 3.
        bounds = calibrate_rmssd_bounds([(10, 20)] * 100)
        assert bounds == {"ceiling_ms": 50.0, "ratio_max": 3.0, "calibrated": True, "n": 100}

    def test_mid_range_user_gets_personal_bounds(self):
        # 30 * 2.25 = 67.5 ms; ratio 3.0 * 2.5 = 7.5
        bounds = calibrate_rmssd_bounds([(30, 10)] * 100)
        assert (bounds["ceiling_ms"], bounds["ratio_max"]) == (67.5, 7.5)

    def test_high_rmssd_user_is_capped_at_absolute(self):
        assert calibrate_rmssd_bounds([(100, 50)] * 100)["ceiling_ms"] == 200.0

    def test_a_few_artifacts_do_not_raise_the_ceiling(self):
        # The history being calibrated from may itself be contaminated. Three
        # 120 ms junk nights among 100 real 10 ms nights leave P95 at 10.
        history = [(10, 20)] * 100 + [(120, 20)] * 3
        bounds = calibrate_rmssd_bounds(history)
        assert bounds["ceiling_ms"] == 50.0
        assert rmssd_is_implausible(120, 20, bounds)[0] is True

    def test_ratio_needs_its_own_sample_count(self):
        # RMSSD synced without SDNN clears the RMSSD threshold but has no ratios.
        assert calibrate_rmssd_bounds([(30, None)] * 100)["ratio_max"] == 10.0

    def test_implausibly_low_sdnn_is_left_out_of_the_ratio(self):
        assert calibrate_rmssd_bounds([(30, 1.0)] * 100)["ratio_max"] == 10.0


class TestRMSSDIsImplausible:
    PERSONAL = {"ceiling_ms": 50.0, "ratio_max": 3.0, "calibrated": True}

    def test_above_ceiling(self):
        bad, why = rmssd_is_implausible(78, None, self.PERSONAL)
        assert bad is True
        assert "personal ceiling 50.0" in why

    def test_at_ceiling_is_allowed(self):
        assert rmssd_is_implausible(50.0, None, self.PERSONAL) == (False, "")

    def test_ratio_artifact_signature(self):
        bad, why = rmssd_is_implausible(40, 10, self.PERSONAL)
        assert bad is True
        assert "artifact signature" in why

    def test_ratio_check_skipped_without_trustworthy_sdnn(self):
        assert rmssd_is_implausible(40, None, self.PERSONAL) == (False, "")
        assert rmssd_is_implausible(40, 2.0, self.PERSONAL) == (False, "")
        assert rmssd_is_implausible(40, "abc", self.PERSONAL) == (False, "")

    def test_empty_bounds_fall_back_to_defaults(self):
        bad, why = rmssd_is_implausible(250, None, {})
        assert bad is True
        assert "default ceiling 200.0" in why
