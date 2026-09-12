# -*- coding: utf-8 -*-
"""The two derived scores.

These check the ORDER of the answers, not their exact values: the weights are
one athlete's calibration and are meant to be re-tuned, but the relationships
between cases are the design and must not move.
"""
import copy

import pytest

from training_log import wellness


def night(hours, deep=0.15, rem=0.20, awake=0.02, **kw):
    s = int(hours * 3600)
    o = {"sleep_s": s, "deep_s": int(s * deep), "rem_s": int(s * rem),
         "awake_s": int(s * awake)}
    o.update(kw)
    return o


def day(**kw):
    """A day with everything present and nothing wrong with it."""
    o = night(7.8)
    o.update({"hrv": 70, "hrv_weekly": 70, "b_low": 60, "b_high": 85,
              "low_up": 50, "rhr": 45.0, "resp": 14.0, "rest_pct": 30,
              "bb_charged": 85, "stress_hist": 80, "sleep_hist": 90,
              "load_explained": 0.0, "load_reason": ""})
    o.update(kw)
    return o


BASE = {"rhr": 45.0, "resp": 14.0}


@pytest.fixture()
def k(athlete):
    return wellness.params(athlete)


# --------------------------------------------------------------------------- #
# sleep                                                                        #
# --------------------------------------------------------------------------- #
def test_a_night_with_no_data_is_not_scored():
    assert wellness.sleep_score({}) == (None, "no-data", "")


def test_perfect_stages_cannot_rescue_a_short_night():
    short = wellness.sleep_score(night(3.0, deep=0.25, rem=0.25, awake=0.0))
    full = wellness.sleep_score(night(8.0, deep=0.25, rem=0.25, awake=0.0))
    assert short[0] < full[0]
    assert full[1] == "excellent"
    assert short[1] != "excellent"


def test_broken_sleep_scores_below_continuous_sleep():
    calm = wellness.sleep_score(night(7.5, awake=0.01))
    broken = wellness.sleep_score(night(7.5, awake=0.15))
    assert broken[0] < calm[0]


# --------------------------------------------------------------------------- #
# condition                                                                    #
# --------------------------------------------------------------------------- #
def test_a_clean_day_scores_at_the_top(k):
    score, label, _ = wellness.condition_score(day(), BASE, k)
    assert score >= k["lb_good"]
    assert label.startswith("strong")
    assert "*" not in label                      # nothing was missing


def test_a_day_without_hrv_or_sleep_is_not_scored(k):
    o = day()
    o.pop("hrv"), o.pop("hrv_weekly")
    score, label, why = wellness.condition_score(o, BASE, k)
    assert score is None
    assert label == "no-data"
    assert "HRV" in why


def tier(label):
    """0 (best) .. 3 (worst) from a label that may carry a bracket and a star."""
    for i, name in enumerate(wellness.LABELS):
        if label.startswith(name):
            return i
    raise AssertionError(f"unrecognised label {label!r}")


def test_the_same_hrv_drop_is_judged_by_what_explains_it(k):
    """The core of the model: fatigue is forgiven, an unexplained drop is not."""
    crashed = day(hrv=30, hrv_weekly=30)
    explained = wellness.condition_score(
        dict(crashed, load_explained=1.0), BASE, k)
    unexplained = wellness.condition_score(
        dict(crashed, load_explained=0.0), BASE, k)
    assert explained[0] > unexplained[0]
    assert tier(explained[1]) < tier(unexplained[1])


def test_an_unexplained_crash_reaches_the_warning_labels(k):
    crashed = day(hrv=30, hrv_weekly=30, load_explained=0.0)
    score, label, _ = wellness.condition_score(crashed, BASE, k)
    assert score < k["lb_fatigue"]
    assert label.startswith(("watch", "unwell"))


def test_a_partial_explanation_lands_between_the_two(k):
    crashed = day(hrv=30, hrv_weekly=30)
    half = wellness.condition_score(dict(crashed, load_explained=0.5), BASE, k)[0]
    full = wellness.condition_score(dict(crashed, load_explained=1.0), BASE, k)[0]
    none = wellness.condition_score(dict(crashed, load_explained=0.0), BASE, k)[0]
    assert none < half < full


def test_training_never_excuses_a_short_night(k):
    """Sleep is deducted identically however hard yesterday was."""
    o = day(**night(3.5))
    tired = wellness.condition_score(dict(o, load_explained=1.0), BASE, k)[0]
    fresh = wellness.condition_score(dict(o, load_explained=0.0), BASE, k)[0]
    assert tired == fresh                        # the HRV term is not engaged
    assert tired < wellness.condition_score(day(), BASE, k)[0]


def test_a_raised_resting_heart_rate_is_never_excused(k):
    ill = day(rhr=52.0)
    assert (wellness.condition_score(dict(ill, load_explained=1.0), BASE, k)[0]
            < wellness.condition_score(day(), BASE, k)[0])


def test_missing_minor_inputs_are_marked_not_scored_as_bad(k):
    o = day()
    o["resp"] = None
    marked = wellness.condition_score(o, BASE, k)
    assert marked[1].endswith("*")
    assert marked[0] == wellness.condition_score(day(), BASE, k)[0]


def test_the_biggest_deduction_is_named_in_the_label(k):
    o = day(rhr=55.0)
    label = wellness.condition_score(o, BASE, k)[1]
    assert "RHR" in label


def test_scoring_does_not_mutate_its_input(k):
    o = day(hrv=30, hrv_weekly=30)
    before = copy.deepcopy(o)
    wellness.condition_score(o, BASE, k)
    assert o == before
