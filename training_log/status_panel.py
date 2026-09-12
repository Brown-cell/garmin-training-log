# -*- coding: utf-8 -*-
"""Rebuild the Status tab: one screen answering "where am I right now?".

Four zones, conclusion first and evidence below:

  1 Verdict   is the race window open, what phase, this week's load target,
              the next checkpoint
  2 Now       the current numbers: CTL / ATL / TSB, ACWR, VO2max, race
              predictions, lactate threshold, condition and HRV seven-day means
  3 Weekly    the last twelve weeks, newest first
  4 Macro     the year by month, and where CTL sits in its own range

Everything is computed here and written as static values, with no live
spreadsheet formulas. The tab is rebuilt after every nightly refresh, so it is
never more than a day stale, and there is no formula-error surface at all.

Three rules this dashboard follows, each learned from being wrong once:

  * **Refuse to judge on incomplete data.** If any day inside the seven-day
    fatigue window was not imported, TSB is an estimate, and the verdict is
    withheld rather than guessed. A sync gap once opened the race window by
    itself.
  * **Deltas before absolutes** for the predictions. Garmin's race-time
    estimates can sit a long way from what an athlete actually runs, while
    their movement over four weeks is still informative.
  * **Say which band is doing the judging.** The condition score judges HRV
    against Garmin's own personal band, not against a standard deviation
    computed from the log. The two disagree, so both are shown and the one in
    use is named.

  python -m training_log.status_panel          # write (the nightly path)
  python -m training_log.status_panel --dry-run
"""
import argparse
import calendar
import datetime as dt
import math
import sys

from . import config, garmin_fetch, log_io, pmc

TAB = config.STATUS_TAB
NCOL = 8

# `kind` values that are NOT a quality session.
EASY = {"", "jog", "off", "rest"}

# Columns that prove a day was actually imported: positive when real, empty
# otherwise. See pmc.day_fields for why columns where 0 is meaningful cannot
# serve as evidence.
SYNC_EVIDENCE = ("steps", "sleep_h", "load")

# Garmin's own training-status phrases, shortened for a one-cell display.
STATUS_TEXT = {
    "RECOVERY": "Recovery", "PRODUCTIVE": "Productive",
    "MAINTAINING": "Maintaining", "OVERREACHING": "Overreaching",
    "DETRAINING": "Detraining", "UNPRODUCTIVE": "Unproductive",
    "PEAKING": "Peaking", "NO": "no status",
}


def rgb(hexs):
    return {"red": int(hexs[0:2], 16) / 255, "green": int(hexs[2:4], 16) / 255,
            "blue": int(hexs[4:6], 16) / 255}


B1, B4, NAVY = rgb("E8F0FE"), rgb("3D85C6"), rgb("0B5394")
BORDER, WHITE = rgb("D5E3F7"), rgb("FFFFFF")
AMBER, RED = rgb("9A6700"), rgb("A61C00")

fnum = pmc.fnum


# --------------------------------------------------------------------------- #
# data                                                                         #
# --------------------------------------------------------------------------- #
def read_log(sh):
    """Log rows -> {date: dict(race, kind, km, hrv, cond, acwr, load, ...)}.

    Columns are resolved by header NAME. Resolving them by position means that
    inserting a column silently starts reading its neighbour, and a dashboard
    reading the wrong column still renders perfectly.
    """
    grid = sh.worksheet(log_io.TAB).get_all_values()
    hdr = grid[0] if grid else []
    ix = {str(name).strip(): i for i, name in enumerate(hdr) if str(name).strip()}
    ev = [ix[k] for k in SYNC_EVIDENCE if k in ix]
    race_col = ix.get("event", ix.get("plan"))
    days = {}
    for r in grid[1:]:
        if not r or not r[0].strip():
            continue
        try:
            d = dt.date.fromisoformat(r[0].strip())
        except ValueError:
            continue

        def g(i):
            return r[i] if (i is not None and i < len(r)) else ""

        load, load_unknown, synced = pmc.day_fields(
            g(ix.get("load")), g(ix.get("kind")), [g(i) for i in ev])
        days[d] = {"race": g(race_col).strip(),
                   "kind": g(ix.get("kind")).strip().lower(),
                   "km": fnum(g(ix.get("km"))) or 0.0,
                   "hrv": fnum(g(ix.get("HRV"))),
                   "cond": fnum(g(ix.get("cond_score"))),
                   "acwr": fnum(g(ix.get("ACWR"))),
                   "load": load, "load_unknown": load_unknown, "synced": synced}
    return days


def last_allout(days, today, allout_load):
    """(date, days ago) of the most recent maximal effort, else (None, None).

    Decided from the session label, not from the hand-written race column: that
    column is where events of every kind get written, including ones that are
    not races at all, while the label column is produced by the classifier and
    means exactly one thing. Anaerobic sessions need a load floor as well, so a
    short set of strides is not counted as a maximal effort.
    """
    best = None
    for d, x in days.items():
        if d > today:
            continue
        kind = str(x.get("kind", ""))
        if "race" in kind or ("anaerobic" in kind
                              and (x.get("load") or 0.0) >= allout_load):
            if best is None or d > best:
                best = d
    return (best, (today - best).days) if best else (None, None)


def allout_note(days, today, allout_load, window):
    """The warning text while inside the window after a maximal effort.

    Every autonomic metric on this dashboard (TSB, readiness, HRV, resting
    heart rate, the condition score) is measuring the same recovery, so in
    the days after an all-out effort they agree with each other and are all
    wrong together. Five green lights on one morning are one green light.
    """
    d, n = last_allout(days, today, allout_load)
    if d is None or n > window:
        return None
    return (f"{n} days since the all-out effort of {d.month}/{d.day}. Inside "
            f"this window, do not judge on TSB (fitness minus fatigue), "
            f"readiness, HRV, resting heart rate or the condition score: all "
            f"five measure the same autonomic recovery, and for {window} days "
            f"they read 'fully recovered' together. Go by how many days have "
            f"passed and by how the legs actually feel.")


def weeks_of(days, bd, today, n=26):
    """The last n Monday-Sunday weeks, oldest first."""
    mon = today - dt.timedelta(days=today.weekday())
    out = []
    for i in range(n - 1, -1, -1):
        w0 = mon - dt.timedelta(weeks=i)
        w1 = min(w0 + dt.timedelta(days=6), today)
        span = [days.get(w0 + dt.timedelta(days=j), {})
                for j in range((w1 - w0).days + 1)]
        conds = [x["cond"] for x in span if x.get("cond") is not None]
        hrvs = [x["hrv"] for x in span if x.get("hrv") is not None]
        races = [x["race"] for x in span if x.get("race")]
        c, a = bd.get(w1, (None, None))
        out.append({
            "w0": w0, "cur": w0 == mon,
            "km": round(sum(x.get("km", 0) or 0 for x in span), 1),
            "load": round(sum(x.get("load", 0) or 0 for x in span)),
            "pts": sum(1 for x in span if x.get("kind", "") not in EASY),
            "cond": round(sum(conds) / len(conds)) if conds else None,
            "hrv": round(sum(hrvs) / len(hrvs)) if hrvs else None,
            "ctl": round(c) if c is not None else None,
            "tsb": round(c - a) if c is not None else None,
            "races": " / ".join(dict.fromkeys(races)),
        })
    return out


def months_of(days, bd, today):
    """This calendar year month by month, up to the current month."""
    out = []
    year = today.year
    for m in range(1, today.month + 1):
        m0 = dt.date(year, m, 1)
        last = (dt.date(year, m + 1, 1) - dt.timedelta(days=1)
                if m < 12 else dt.date(year, 12, 31))
        m1 = min(last, today)
        span = [(d, x) for d, x in days.items() if m0 <= d <= m1]
        races = [x["race"] for d, x in sorted(span) if x["race"]]
        c, _ = bd.get(m1, (None, None))
        out.append({"m": m, "end": m1,
                    "km": round(sum(x["km"] for _, x in span), 1),
                    "load": round(sum(x["load"] for _, x in span)),
                    "ctl": round(c) if c is not None else None,
                    "races": " / ".join(dict.fromkeys(races))})
    return out


def hrv_band(g, today, back=7):
    """Garmin's own HRV band (balancedLow, balancedUpper), or None.

    This is the band the condition score judges against. A mean and standard
    deviation computed from the log is a different statistic, and showing only
    that one lets two screens read the same HRV in opposite directions.
    """
    for i in range(back):
        d = (today - dt.timedelta(days=i)).isoformat()
        summary = (garmin_fetch._try(g.get_hrv_data, d) or {}).get("hrvSummary") or {}
        base = summary.get("baseline") or {}
        lo, hi = base.get("balancedLow"), base.get("balancedUpper")
        if lo and hi:
            return lo, hi
    return None


def vo2_at(g, iso):
    r = garmin_fetch._try(g.get_max_metrics, iso)
    for it in (r if isinstance(r, list) else [r] if r else []):
        if isinstance(it, dict):
            gen = it.get("generic") or {}
            v = gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue")
            if v:
                return round(float(v), 1)
    return None


def race_pred_hist(g, d0, d1):
    """[{calendarDate, time5K, time10K}]; the API shape is version dependent."""
    for typ in ("daily", "monthly", None):
        r = garmin_fetch._try(g.get_race_predictions, d0.isoformat(),
                              d1.isoformat(), typ)
        if isinstance(r, list) and r and isinstance(r[0], dict):
            return [x for x in r if x.get("calendarDate")]
    return []


def hms(sec):
    if not sec:
        return ""
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def snum(v, nd=0):
    """A delta as a NUMBER. The leading '+' comes from the number format, not
    from the string: USER_ENTERED reads a leading '+' as a formula."""
    return "" if v is None else round(v, nd)


P0 = '+0;-0;0'                        # signed-integer display pattern


def status_text(phrase):
    if not phrase:
        return "-"
    for k, v in STATUS_TEXT.items():
        if phrase.startswith(k):
            return v
    return phrase


def spark(vals, chart="line", ymin=None, ymax=None):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return ""
    if chart == "line":
        o = '"charttype","line";"color","#0B5394";"linewidth",2'
    else:
        o = '"charttype","column";"color","#3D85C6"'
    if ymin is not None:
        o += f';"ymin",{ymin}'
    if ymax is not None:
        o += f';"ymax",{ymax}'
    return "=SPARKLINE({" + ",".join(str(v) for v in vals) + "},{" + o + "})"


# --------------------------------------------------------------------------- #
# verdict (rule-based so it is reproducible; the thresholds are inferences)    #
# --------------------------------------------------------------------------- #
def verdict(ctl, tsb, pct, slope, open_pct, gaps7=()):
    if gaps7:
        ds = ", ".join(f"{d.month}/{d.day}" for d in sorted(gaps7))
        return False, (f"Judgement withheld: days in the last week were never "
                       f"imported ({ds}), so TSB is an estimate and the window "
                       f"cannot be called. Check the watch sync, then refresh.")
    if pct >= open_pct and tsb > 0:
        return True, ("Open: CTL near its high, fatigue cleared. Racing now "
                      "should go well (inference).")
    if pct >= open_pct and tsb > -10:
        return False, ("Nearly open: fitness is high. A few easier days should "
                       "open the window (inference).")
    if slope > 2:
        return False, ("Closed, building: CTL is climbing again. The window is "
                       "still ahead; keep stacking weeks (inference).")
    return False, ("Closed, rebuilding: chronic load has to come back first "
                   "(inference).")


def hrv_note(band, hm, hs):
    """The band in use first; the log's own statistics after, named as such."""
    parts = []
    if band:
        parts.append(f"personal band {band[0]:.0f}-{band[1]:.0f} "
                     f"(Garmin; the condition score judges against this)")
    if hm and hs:
        parts.append(f"last 28 days {hm:.0f}+/-{hs:.0f} (spread, not a band)")
    return " / ".join(parts)


def phase_text(pct, slope, since):
    trend = "rising" if slope > 2 else ("falling" if slope < -2 else "flat")
    return (f"CTL is {trend}, {slope:+.0f} against four weeks ago, and sits at "
            f"the {pct*100:.0f}th percentile of its range since {since}.")


# --------------------------------------------------------------------------- #
# grid                                                                         #
# --------------------------------------------------------------------------- #
def build(days, bd, today, panel, extras, athlete, unknown=frozenset(), dry=False):
    st = athlete.section("status")
    _, _, _, pct_from = pmc.params(athlete)
    acwr_lo, acwr_hi = st["acwr_safe"]
    tgt_pct = float(st["week_load_target_pct"])
    allout_load, allout_window = st["all_out_load"], st["all_out_window_days"]

    weeks = weeks_of(days, bd, today)
    months = months_of(days, bd, today)
    ctl, atl = bd[today]
    tsb = ctl - atl
    c4, a4 = bd.get(today - dt.timedelta(days=28), (None, None))
    rng = [c for d, (c, _) in bd.items() if d >= pct_from] or [ctl]
    lo, hi = min(rng), max(rng)
    pct = (ctl - lo) / (hi - lo) if hi > lo else 0.0
    slope = ctl - c4 if c4 is not None else 0.0
    # A gap inside the 7-day fatigue window stops the verdict; a gap further
    # back only earns a note.
    gaps7 = {d for d in unknown if 0 <= (today - d).days <= 6}
    gaps42 = {d for d in unknown if 0 <= (today - d).days <= 41}
    is_open, vtext = verdict(ctl, tsb, pct, slope, float(st["ctl_percentile_open"]),
                             gaps7)

    def wavg(back0, back1, key):
        vs = [days.get(today - dt.timedelta(days=i), {}).get(key)
              for i in range(back0, back1 + 1)]
        vs = [v for v in vs if v is not None]
        return (sum(vs) / len(vs)) if vs else None

    cond7, cond7p = wavg(0, 6, "cond"), wavg(28, 34, "cond")
    hrv7, hrv7p = wavg(0, 6, "hrv"), wavg(28, 34, "hrv")
    h28 = [days.get(today - dt.timedelta(days=i), {}).get("hrv") for i in range(1, 29)]
    h28 = [v for v in h28 if v is not None]
    hm = sum(h28) / len(h28) if h28 else None
    hs = (math.sqrt(sum((v - hm) ** 2 for v in h28) / (len(h28) - 1))
          if h28 and len(h28) > 2 else None)
    acwr_now = next((days[today - dt.timedelta(days=i)]["acwr"] for i in range(0, 8)
                     if days.get(today - dt.timedelta(days=i), {}).get("acwr")
                     is not None), None)
    acwr_p = next((days[today - dt.timedelta(days=28 + i)]["acwr"] for i in range(0, 4)
                   if days.get(today - dt.timedelta(days=28 + i), {}).get("acwr")
                   is not None), None)

    wk_now = weeks[-1]
    tgt_lo = int(round(ctl * 7 * (1 - tgt_pct), -1))
    tgt_hi = int(round(ctl * 7 * (1 + tgt_pct), -1))
    nxt = next(((dt.date.fromisoformat(d), t) for d, t in athlete.races
                if dt.date.fromisoformat(d) >= today), None)

    rows, kind, cellfmt, numfmt = [], [], [], []

    def add(row, k):
        rows.append((row + [""] * NCOL)[:NCOL])
        kind.append(k)
        return len(rows)                  # 1-based sheet row

    add([f"Status: training dashboard (rebuilt {today})"], "title")

    # -- zone 1: verdict ---------------------------------------------------- #
    add(["1. Verdict: what to do now"], "band")
    r = add(["race window", vtext], "z1")
    if is_open:
        cellfmt.append((r - 1, 1, {"foregroundColor": NAVY, "bold": True}))
    if gaps42:
        ds = ", ".join(f"{d.month}/{d.day}" for d in sorted(gaps42)[:8])
        more = f" and {len(gaps42) - 8} more" if len(gaps42) > 8 else ""
        r = add(["missing data", f"{len(gaps42)} days never synced ({ds}{more}). "
                 f"CTL / ATL / TSB are estimates that charge those days as a "
                 f"typical day."], "z1")
        cellfmt.append((r - 1, 1, {"foregroundColor": RED, "bold": True}))
    anote = allout_note(days, today, allout_load, allout_window)
    if anote:
        r = add(["after an all-out effort", anote], "z1")
        cellfmt.append((r - 1, 1, {"foregroundColor": AMBER, "bold": True}))
    add(["phase", phase_text(pct, slope, pct_from)], "z1")
    gw = sum(1 for d in unknown if wk_now["w0"] <= d <= today)
    add(["this week's load", f"{wk_now['load']} so far (Monday to today"
         + (f", understated: {gw} days never synced" if gw else "")
         + f") against a target band of {tgt_lo}-{tgt_hi} "
         + f"(CTL x 7, plus or minus {tgt_pct*100:.0f}%, inference)"], "z1")
    add(["next checkpoint", (f"{nxt[0].month}/{nxt[0].day} {nxt[1]}, "
                             f"{(nxt[0] - today).days} days away"
                             if nxt else "none scheduled")], "z1")

    # -- zone 2: now -------------------------------------------------------- #
    add(["2. Now: the headline numbers (delta = against four weeks ago)"], "band")
    add(["metric", "value", "4wk delta", "26 weeks", "", "", "notes"], "sub")
    wctl = [w["ctl"] for w in weeks]
    wtsb = [w["tsb"] for w in weeks]
    wcond = [w["cond"] for w in weeks]
    whrv = [w["hrv"] for w in weeks]
    p5, p5d, p10, p10d, vo2, vo2d, _, hband = extras
    SEC = '+0"s";-0"s";0'
    ctl_days, atl_days, _, _ = pmc.params(athlete)
    z2 = [  # label, value, value pattern, delta, delta pattern, spark, note, mark
        ("CTL (fitness)", round(ctl), None, snum(slope), P0, spark(wctl, ymin=0),
         f"{ctl_days}-day exponential mean of load", None),
        ("ATL (fatigue)", round(atl), None,
         snum(atl - a4) if a4 is not None else "", P0, "",
         f"{atl_days}-day exponential mean of load", None),
        ("TSB (form)", snum(tsb), P0,
         snum(tsb - (c4 - a4)) if c4 is not None else "", P0,
         spark(wtsb, chart="column"), "positive = fatigue has cleared", None),
        ("ACWR", acwr_now if acwr_now is not None else "", None,
         snum(acwr_now - acwr_p, 2) if None not in (acwr_now, acwr_p) else "",
         "+0.00;-0.00;0", "", f"{acwr_lo}-{acwr_hi} is the safe range",
         "out" if acwr_now is not None
         and not (acwr_lo <= acwr_now <= acwr_hi) else None),
        ("VO2max", vo2 if vo2 else "", None,
         snum(vo2d, 1) if vo2d is not None else "", "+0.0;-0.0;0", "", "", None),
        ("5k prediction", "'" + hms(p5) if p5 else "", None, snum(p5d), SEC, "",
         "read the delta, not the absolute time", None),
        ("10k prediction", "'" + hms(p10) if p10 else "", None, snum(p10d), SEC,
         "", "same (a minus sign means faster)", None),
        ("LT", (f"{panel.get('lt_hr')}bpm, {panel.get('lt_pace') or '-'}"
                if panel.get("lt_hr") else ""), None, "", None, "",
         "the anchor for threshold work", None),
        ("Garmin status", status_text(panel.get("train_status")), None, "", None,
         "", "for reference only", None),
        ("condition (7d mean)", round(cond7) if cond7 is not None else "", None,
         snum(cond7 - cond7p) if None not in (cond7, cond7p) else "", P0,
         spark(wcond, ymin=40, ymax=100),
         "90+ strong / 66+ fine / 50+ watch", "cond"),
        ("HRV (7d mean)", round(hrv7) if hrv7 is not None else "", None,
         snum(hrv7 - hrv7p) if None not in (hrv7, hrv7p) else "", P0,
         spark(whrv), hrv_note(hband, hm, hs), None),
    ]
    if dry:
        # A dry run does not call Garmin, so these cells are empty because they
        # were not fetched -- not because there is no data.
        r = add(["DRY RUN", "VO2max, LT, the 5k/10k predictions and the Garmin "
                 "status were not fetched. The blanks below mean 'not "
                 "requested on this run', not 'no data'."],
                "z2")
        cellfmt.append((r - 1, 1, {"foregroundColor": AMBER, "italic": True}))
    for label, val, bpat, dlt, dpat, sp, note_txt, mark in z2:
        r = add([label, val, dlt, sp, "", "", note_txt], "z2")
        if bpat:
            numfmt.append((r - 1, 1, bpat))
        if dpat and dlt != "":
            numfmt.append((r - 1, 2, dpat))
        if mark == "out":
            cellfmt.append((r - 1, 1, {"foregroundColor": NAVY, "bold": True}))
        if mark == "cond" and cond7 is not None:
            if cond7 < 60:
                cellfmt.append((r - 1, 1, {"foregroundColor": RED, "bold": True}))
            elif cond7 < 80:
                cellfmt.append((r - 1, 1, {"foregroundColor": AMBER, "bold": True}))
            elif cond7 < 90:
                cellfmt.append((r - 1, 1, {"bold": True}))

    # -- zone 3: weekly ----------------------------------------------------- #
    add(["3. Weekly: the last twelve weeks, newest first"], "band")
    add(["week", "km", "load", "CTL", "TSB", "condition", "quality",
         "races and events"], "sub")
    for w in reversed(weeks[-12:]):
        lbl = f"{w['w0'].month}/{w['w0'].day}-" + (" (this week)" if w["cur"]
                                                   else "")
        r = add([lbl, w["km"], w["load"], w["ctl"] if w["ctl"] is not None else "",
                 w["tsb"] if w["tsb"] is not None else "",
                 w["cond"] if w["cond"] is not None else "", w["pts"],
                 w["races"]], "z3cur" if w["cur"] else "z3")
        if w["tsb"] is not None:
            numfmt.append((r - 1, 4, P0))
        if w["cond"] is not None and w["cond"] < 80:
            cellfmt.append((r - 1, 5, {"foregroundColor":
                                       RED if w["cond"] < 60 else AMBER,
                                       "bold": True}))

    # -- zone 4: macro ------------------------------------------------------ #
    add([f"4. Macro: {today.year} month by month"], "band")
    add(["month", "km", "load", "CTL at month end", "VO2max", "", "races"],
        "sub")
    mvo2 = extras[6] or {}
    for mo in months:
        add([calendar.month_abbr[mo["m"]], mo["km"], mo["load"],
             mo["ctl"] if mo["ctl"] is not None else "",
             mvo2.get(mo["m"], ""), "", mo["races"]], "z4")
    add([f"CTL is {ctl:.0f}, the {pct*100:.0f}th percentile of this year's "
         f"range {lo:.0f}-{hi:.0f} (since {pct_from})"], "pct")

    # -- footnotes ---------------------------------------------------------- #
    add([], "gap")
    for t in ("5k/10k predictions: the absolute times can sit a long way from "
              "what this athlete actually runs, so only the delta against "
              "four weeks ago is worth reading.",
              f"CTL = fitness ({ctl_days}-day exponential mean of load) / "
              f"ATL = fatigue ({atl_days}-day) / TSB = CTL minus ATL = form. "
              f"Every threshold here is an inference awaiting calibration.",
              f"The race window opens (inference) when CTL is at or above the "
              f"{float(st['ctl_percentile_open'])*100:.0f}th percentile of its "
              f"range this year and TSB is positive. For the week's load, aim "
              f"at the target band above (CTL x 7)."):
        add([t], "foot")
    return rows, kind, cellfmt, numfmt


# --------------------------------------------------------------------------- #
# formatting                                                                   #
# --------------------------------------------------------------------------- #
def gr(sid, r1, r2, c1, c2):
    return {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def repeat(sid, r1, r2, c1, c2, fmt):
    fields = "userEnteredFormat(" + ",".join(sorted(fmt)) + ")"
    return {"repeatCell": {"range": gr(sid, r1, r2, c1, c2),
                           "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def note(sid, r, c, text):
    return {"updateCells": {"start": {"sheetId": sid, "rowIndex": r,
                                      "columnIndex": c},
                            "rows": [{"values": [{"note": text}]}],
                            "fields": "note"}}


def fmt_requests(sid, kind, cellfmt, numfmt, ctl_days, atl_days, season_start):
    n = len(kind)
    q = [{"updateSheetProperties": {"properties": {
        "sheetId": sid, "tabColor": B4,
        "gridProperties": {"frozenRowCount": 1, "hideGridlines": True}},
        "fields": "tabColor,gridProperties(frozenRowCount,hideGridlines)"}}]
    q.append(repeat(sid, 0, n, 0, NCOL, {"wrapStrategy": "CLIP",
                                         "textFormat": {"fontSize": 10}}))
    for i, k in enumerate(kind):
        if k == "title":
            q.append({"mergeCells": {"range": gr(sid, i, i + 1, 0, NCOL),
                                     "mergeType": "MERGE_ALL"}})
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B4, "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": WHITE, "bold": True,
                               "fontSize": 12}}))
        elif k == "band":
            q.append({"mergeCells": {"range": gr(sid, i, i + 1, 0, NCOL),
                                     "mergeType": "MERGE_ALL"}})
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B4, "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": WHITE, "bold": True,
                               "fontSize": 10}}))
        elif k == "sub":
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B1,
                "textFormat": {"foregroundColor": NAVY, "bold": True,
                               "fontSize": 10}}))
        elif k in ("z1", "pct"):
            q.append(repeat(sid, i, i + 1, 0, 1, {
                "textFormat": {"foregroundColor": NAVY, "bold": True,
                               "fontSize": 10}}))
        elif k == "z3cur":
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B1, "textFormat": {"fontSize": 10}}))
        elif k == "foot":
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "textFormat": {"foregroundColor": NAVY, "italic": True,
                               "fontSize": 10}}))
        if k == "z2":
            q.append({"mergeCells": {"range": gr(sid, i, i + 1, 3, 6),
                                     "mergeType": "MERGE_ALL"}})
    z2r = [i for i, k in enumerate(kind) if k == "z2"]
    z3r = [i for i, k in enumerate(kind) if k in ("z3", "z3cur")]
    z4r = [i for i, k in enumerate(kind) if k == "z4"]
    for rr in (z2r, z3r, z4r):
        if rr:
            q.append(repeat(sid, rr[0], rr[-1] + 1, 0, 1, {
                "textFormat": {"foregroundColor": NAVY, "fontSize": 10}}))
    if z2r:
        q.append(repeat(sid, z2r[0], z2r[-1] + 1, 1, 3,
                        {"horizontalAlignment": "RIGHT"}))
    if z3r:
        q.append(repeat(sid, z3r[0], z3r[-1] + 1, 1, 2,
                        {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
        q.append({"updateBorders": {"range": gr(sid, z3r[0], z3r[-1] + 1, 0, NCOL),
                                    "innerHorizontal": {"style": "SOLID",
                                                        "color": BORDER}}})
    if z4r:
        q.append(repeat(sid, z4r[0], z4r[-1] + 1, 1, 2,
                        {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
        q.append(repeat(sid, z4r[0], z4r[-1] + 1, 4, 5,
                        {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
        q.append({"updateBorders": {"range": gr(sid, z4r[0], z4r[-1] + 1, 0, NCOL),
                                    "innerHorizontal": {"style": "SOLID",
                                                        "color": BORDER}}})
    for r0, c0, tf in cellfmt:
        q.append(repeat(sid, r0, r0 + 1, c0, c0 + 1, {"textFormat": tf}))
    for r0, c0, pat in numfmt:
        q.append(repeat(sid, r0, r0 + 1, c0, c0 + 1,
                        {"numberFormat": {"type": "NUMBER", "pattern": pat}}))
    lbl_note = {
        "CTL (fitness)": f"Chronic Training Load: the {ctl_days}-day "
                         f"exponential mean of daily load. The series is "
                         f"seeded at zero on {season_start}, so the first few "
                         f"weeks read a little low (inference).",
        "TSB (form)": "Training Stress Balance = CTL minus ATL. Positive means "
                      "fitness is ahead of fatigue, which is the state to race "
                      "in. The absolute cutoffs are still being calibrated "
                      "(inference).",
        "5k prediction": "Garmin's estimate. For some athletes the absolute "
                         "time sits far from what they actually run, so read "
                         "only the change against four weeks ago.",
    }
    q.append(note(sid, 0, 0, "Rebuilt by the nightly job. There are no "
                             "hand-written columns on this tab."))
    widths = [118, 78, 78, 78, 78, 78, 130, 300]
    for i, w in enumerate(widths):
        q.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    q.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0,
                  "endIndex": 1},
        "properties": {"pixelSize": 34}, "fields": "pixelSize"}})
    return q, lbl_note


# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m training_log.status_panel",
        description="Rebuild the Status dashboard tab from the Log tab.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned grid; touch neither Garmin nor the sheet")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    dry = args.dry_run
    today = dt.date.today()
    athlete = config.athlete()
    ctl_days, atl_days, season_start, _ = pmc.params(athlete)

    sh = config.spreadsheet()
    days = read_log(sh)
    bd, unknown = pmc.series(days, today, ctl_days, atl_days, start=season_start)

    panel, extras_vo2 = {}, {}
    p5 = p5d = p10 = p10d = vo2 = vo2d = hband = None
    if not dry:
        g = garmin_fetch._try(config.garmin_client)
        if g:
            for i in range(0, 8):
                d = (today - dt.timedelta(days=i)).isoformat()
                cand = garmin_fetch._try(garmin_fetch.fetch_status_panel, g, d) or {}
                if cand.get("vo2max") or cand.get("train_status"):
                    panel = cand
                    break
            p5, p10 = panel.get("race_5k"), panel.get("race_10k")
            hist = race_pred_hist(g, today - dt.timedelta(days=32), today)
            if hist:
                base = min(hist, key=lambda x: abs(
                    (dt.date.fromisoformat(x["calendarDate"])
                     - (today - dt.timedelta(days=28))).days))
                if p5 and base.get("time5K"):
                    p5d = p5 - base["time5K"]
                if p10 and base.get("time10K"):
                    p10d = p10 - base["time10K"]
            vo2 = panel.get("vo2max") or vo2_at(g, today.isoformat())
            v4 = vo2_at(g, (today - dt.timedelta(days=28)).isoformat())
            if vo2 and v4:
                vo2d = round(vo2 - v4, 1)
            for m in range(1, today.month + 1):
                last = ((dt.date(today.year, m + 1, 1) - dt.timedelta(days=1))
                        if m < 12 else dt.date(today.year, 12, 31))
                extras_vo2[m] = vo2_at(g, min(last, today).isoformat()) or ""
            if not extras_vo2.get(today.month) and vo2:
                extras_vo2[today.month] = vo2
            hband = hrv_band(g, today)

    rows, kind, cellfmt, numfmt = build(
        days, bd, today, panel,
        (p5, p5d, p10, p10d, vo2, vo2d, extras_vo2, hband),
        athlete, unknown, dry=dry)
    grid = [[("" if c is None else c) for c in r] for r in rows]
    for r in grid:
        print(" | ".join(str(c)[:30] for c in r))
    if dry:
        print(f"\n[dry run] {len(grid)} rows planned for '{TAB}'.")
        return 0

    old_idx = None
    try:
        old = sh.worksheet(TAB)
        old_idx = old._properties.get("index")
        sh.del_worksheet(old)
    except Exception:
        pass
    ws = sh.add_worksheet(title=TAB, rows=len(grid) + 5, cols=NCOL)
    sid = ws.id
    ws.update(values=grid, range_name=f"A1:H{len(grid)}",
              value_input_option="USER_ENTERED")
    q, lbl_note = fmt_requests(sid, kind, cellfmt, numfmt, ctl_days, atl_days,
                               season_start)
    for i, k in enumerate(kind):
        if k == "z2" and str(grid[i][0]) in lbl_note:
            q.append(note(sid, i, 0, lbl_note[str(grid[i][0])]))
    if old_idx is not None:
        q.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "index": old_idx}, "fields": "index"}})
    sh.batch_update({"requests": q})
    print(f"\nWrote '{TAB}': {len(grid)} rows, formatting applied (sheetId {sid}).")
    return 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
