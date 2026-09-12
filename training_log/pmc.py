# -*- coding: utf-8 -*-
"""The fitness-fatigue model: CTL, ATL and TSB.

A Banister impulse-response model of the kind popularised by the TrainingPeaks
Performance Management Chart. Each day's training load is fed into two
exponentially weighted moving averages with different time constants:

    CTL  chronic training load, 42 days   "fitness"
    ATL  acute training load,    7 days   "fatigue"
    TSB  CTL - ATL                        "form"

    x_today = x_yesterday + (load_today - x_yesterday) * (1 - exp(-1/days))

Garmin's session load is unitless but internally consistent, so comparing a
value against the same athlete's own history is meaningful while comparing it
against a published threshold is not. Treat every absolute TSB cutoff in this
project as an inference awaiting personal calibration.

**A missing day is not a rest day.** Both averages decay towards whatever they
are fed, so scoring an un-synced day as zero load makes fatigue fall faster than
it really did. Two missing days once moved a TSB from -26 to +2.5 and flipped a
race-readiness verdict from "no" to "yes". Days with no evidence of having been
imported are therefore filled with the current CTL (assumed typical) and
returned in a separate set, so the caller can refuse to publish a verdict that
rests on them.
"""
import datetime as dt
import math


DEFAULT_CTL_DAYS = 42
DEFAULT_ATL_DAYS = 7


def params(athlete=None):
    """(ctl_days, atl_days, season_start, percentile_from) from athlete.json."""
    from . import config
    a = athlete or config.athlete()
    s = a.section("pmc")
    return (int(s.get("ctl_days", DEFAULT_CTL_DAYS)),
            int(s.get("atl_days", DEFAULT_ATL_DAYS)),
            dt.date.fromisoformat(s["season_start"]),
            dt.date.fromisoformat(s["percentile_from"]))


def decay(days):
    """The per-day weight of a new load in an `days`-day exponential average."""
    return 1 - math.exp(-1 / days)


def step(prev, load, days):
    """One day of an exponentially weighted moving average."""
    return prev + (load - prev) * decay(days)


def series_from_loads(loads, ctl_days=DEFAULT_CTL_DAYS, atl_days=DEFAULT_ATL_DAYS,
                      ctl0=0.0, atl0=0.0):
    """A list of daily loads -> [(CTL, ATL)], one entry per day, in order.

    `None` in the list means "this day was not imported": it is charged at the
    running CTL, which leaves CTL unchanged by definition and lets ATL relax
    towards it instead of collapsing.
    """
    ctl, atl, out = float(ctl0), float(atl0), []
    for load in loads:
        x = ctl if load is None else float(load)
        ctl = step(ctl, x, ctl_days)
        atl = step(atl, x, atl_days)
        out.append((ctl, atl))
    return out


def tsb(ctl, atl):
    return ctl - atl


def day_fields(load_cell, kind_cell, evidence_cells):
    """One raw Log row -> (load, is the load unknown, was the day imported).

    **This function is the single definition of that rule** -- it is asked the
    same question from three places and a second copy would drift.

    An empty load cell means zero only if the day was imported at all, i.e. it
    is a genuine rest day. A day whose hand-written label says training
    happened but whose load is empty is a day the watch has no activity for,
    and that is unknown rather than zero.

    Evidence of import has to be a column that is positive when real and empty
    otherwise -- steps, sleep hours, load. Columns where zero is meaningful
    (stairs, battery drain, intensity minutes) are useless as evidence: a day
    with nothing but zeroes in them looks synced and rested when it is neither.
    """
    return (fnum(load_cell) or 0.0,
            (not str(load_cell).strip()
             and str(kind_cell).strip().lower() not in ("", "off", "rest")),
            any(fnum(c) for c in evidence_cells))


def fnum(s):
    """Sheet cell -> float, or None when it is not a number."""
    try:
        v = str(s).strip()
        return float(v) if v else None
    except Exception:
        return None


def series(days, today, ctl_days=DEFAULT_CTL_DAYS, atl_days=DEFAULT_ATL_DAYS,
           start=None):
    """{date: row} -> ({date: (CTL, ATL)}, {dates whose load was unknown}).

    Rows are the dicts `status_panel.read_log` builds: `load`, `synced` and
    `load_unknown`. The series is seeded at zero on `start`, so the first weeks
    of a season read low; that is why the percentile comparison in the
    dashboard starts later.
    """
    if start is None:
        start = min(days) if days else today
    out, unknown = {}, set()
    ctl = atl = 0.0
    d = start
    while d <= today:
        row = days.get(d)
        if row is None or not row.get("synced") or row.get("load_unknown"):
            unknown.add(d)
            load = ctl                      # assume a typical day
        else:
            load = row.get("load", 0.0) or 0.0
        ctl = step(ctl, load, ctl_days)
        atl = step(atl, load, atl_days)
        out[d] = (ctl, atl)
        d += dt.timedelta(days=1)
    return out, unknown
