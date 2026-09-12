# -*- coding: utf-8 -*-
"""The fitness-fatigue model, against arithmetic done by hand."""
import datetime as dt
import math

from training_log import pmc


def test_decay_matches_the_definition():
    assert pmc.decay(42) == 1 - math.exp(-1 / 42)
    assert pmc.decay(7) == 1 - math.exp(-1 / 7)


def test_one_step_from_zero_is_load_times_decay():
    assert pmc.step(0.0, 100.0, 7) == 100.0 * (1 - math.exp(-1 / 7))


def test_three_steps_match_the_hand_calculation():
    loads = [100.0, 0.0, 50.0]
    kc, ka = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    ctl = atl = 0.0
    want = []
    for x in loads:
        ctl += (x - ctl) * kc
        atl += (x - atl) * ka
        want.append((ctl, atl))
    got = pmc.series_from_loads(loads)
    assert len(got) == 3
    for (gc, ga), (wc, wa) in zip(got, want):
        assert gc == wc
        assert ga == wa


def test_fatigue_rises_faster_than_fitness():
    """The 7-day average must react harder to one hard day than the 42-day one."""
    ctl, atl = pmc.series_from_loads([200.0])[0]
    assert atl > ctl
    assert pmc.tsb(ctl, atl) < 0


def test_rest_brings_form_positive_again():
    loads = [200.0] + [0.0] * 21
    ctl, atl = pmc.series_from_loads(loads)[-1]
    assert pmc.tsb(ctl, atl) > 0


def test_unknown_day_is_charged_at_current_fitness():
    """A None day must leave CTL exactly where it was."""
    before = pmc.series_from_loads([100.0] * 10)[-1]
    after = pmc.series_from_loads([100.0] * 10 + [None])[-1]
    assert after[0] == before[0]                 # fitness unchanged
    assert after[1] != before[1]                 # fatigue relaxes towards it


def test_day_fields_empty_load_on_a_rest_day_is_zero_not_unknown():
    load, unknown, synced = pmc.day_fields("", "rest", ["8000", "7.5", ""])
    assert load == 0.0
    assert unknown is False
    assert synced is True


def test_day_fields_empty_load_on_a_training_day_is_unknown():
    load, unknown, _ = pmc.day_fields("", "Threshold", ["8000"])
    assert load == 0.0
    assert unknown is True


def test_day_fields_zero_only_evidence_is_not_evidence():
    """Columns where 0 is meaningful cannot prove a day was imported."""
    _, _, synced = pmc.day_fields("", "", ["", "", ""])
    assert synced is False
    _, _, synced = pmc.day_fields("", "", ["0", "0"])
    assert synced is False


def test_series_marks_missing_days_and_keeps_going():
    d0 = dt.date(2026, 3, 1)
    days = {d0 + dt.timedelta(days=i):
            {"load": 80.0, "synced": True, "load_unknown": False}
            for i in range(5)}
    del days[d0 + dt.timedelta(days=2)]          # a day that was never imported
    out, unknown = pmc.series(days, d0 + dt.timedelta(days=4), start=d0)
    assert unknown == {d0 + dt.timedelta(days=2)}
    assert len(out) == 5
    assert out[d0 + dt.timedelta(days=4)][0] > 0


def test_series_respects_the_configured_time_constants(athlete):
    ctl_days, atl_days, season_start, pct_from = pmc.params(athlete)
    assert (ctl_days, atl_days) == (42, 7)
    assert season_start < pct_from
