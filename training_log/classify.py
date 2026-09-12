# -*- coding: utf-8 -*-
"""Turn a day's Garmin activities into the three run columns of the Log tab.

`build_entry()` returns (kind, menu, data, is_workout):

  kind  ("種別")  a label: rest / Jog / Jog + WS / Threshold / VO2 / race /
                  composites such as "Jog + Threshold"
  menu  ("詳細")  what was done: "5x1000m + jog計3.0km"
  data  ("データ") how it went, one line per set: splits, HRmax, watts

The label is decided by three votes, never by pace alone:

  structure  laps are split into reps and sets. A rep is a contiguous run of
             "quality" laps (the watch chops one continuous effort into several
             same-pace auto-laps, so they are merged); reps are separated by
             recovery laps, and consecutive reps of similar length form a set.
             Rep length and the ratio of recovery to work time separate a
             lactate-tolerance session from an anaerobic one.

  heart rate the intensity grade comes from the average HR of the reps,
             against the athlete's own lactate-test anchors in athlete.json.

  power      running power rescues the HR vote. Wrist optical HR drops out
             constantly on track reps (a 2:54/km rep can read 86 bpm on a day
             whose race maximum was 172), and a session graded on that is
             graded on a sensor failure. Power does not drop out.

There is no "something happened here" label. A label that only records
uncertainty cannot be acted on by the rest of the pipeline, and it survives in
the sheet looking exactly like a hand-written classification. Every day
resolves to a real zone or to rest.
"""
from . import config

RUN_TYPES = {"running", "track_running", "trail_running",
             "treadmill_running", "virtual_run"}


# --------------------------------------------------------------------------- #
# formatting (these strings land in the sheet, so they stay in the sheet's     #
# own language: 分 = minutes, jog計 = "jog, total")                            #
# --------------------------------------------------------------------------- #
def fmt_km(m):
    return f"{m/1000:.2f}km"


def fmt_min(s):
    return f"{round(s/60)}分"


def fmt_pace(s, m):
    if not m:
        return ""
    spk = s / (m / 1000.0)
    return f"{int(spk//60)}:{int(round(spk%60)):02d}/km"


def fmt_hr(h):
    return f"HR{int(round(h))}" if h else ""


def fmt_rep(dur):
    """Rep duration: seconds under 100, else m'ss\"."""
    return f"{dur:.0f}" if dur < 100 else f"{int(dur//60)}'{int(round(dur%60)):02d}\""


def local_date(act):
    return act["startTimeLocal"][:10]


def typekey(act):
    return (act.get("activityType") or {}).get("typeKey", "")


# --------------------------------------------------------------------------- #
# gates                                                                        #
# --------------------------------------------------------------------------- #
class Gates:
    """The classifier's thresholds, read from athlete.json's `classifier`."""

    KEYS = ("quality_lap_pace_sec_per_km", "quality_lap_power_w",
            "hr_rescue_min_bpm", "hr_rescue_pace_sec_per_km",
            "hr_dropout_below_bpm", "stride_pace_sec_per_km",
            "power_threshold_w", "power_vo2_w",
            "hr_threshold_bpm", "hr_obla_bpm", "hr_vo2_bpm")

    def __init__(self, **kw):
        for k in self.KEYS:
            if k not in kw:
                raise config.ConfigError(f"'classifier.{k}' is missing.")
            setattr(self, k, float(kw[k]))

    @classmethod
    def from_athlete(cls, athlete=None):
        a = athlete or config.athlete()
        return cls(**a.section("classifier"))


def _gates(gates=None):
    return gates if gates is not None else Gates.from_athlete()


# --------------------------------------------------------------------------- #
# laps -> sets                                                                 #
# --------------------------------------------------------------------------- #
def parse_laps(g, act):
    """Garmin lap DTOs -> the five fields this module uses. [] on any failure."""
    try:
        laps = (g.get_activity_splits(act["activityId"]) or {}).get("lapDTOs", [])
    except Exception:
        laps = []
    out = []
    for lp in laps or []:
        d = lp.get("distance") or 0
        s = lp.get("duration") or 0
        if d <= 0 or s <= 0:
            continue
        out.append({"d": d, "s": s, "pace": s / (d / 1000.0),
                    "aHR": lp.get("averageHR"), "mHR": lp.get("maxHR"),
                    "pw": lp.get("averagePower")})
    return out


def is_quality(lp, gates=None):
    """Is this lap part of the work, rather than a warm-up or a recovery?

    Pace or power qualifies it outright. The HR clause needs BOTH a sustained
    heart rate and a pace, because heart rate alone cannot gate: recovery jogs
    between short reps sit in the 170s while running above 5:15/km, whereas a
    threshold lap run in the heat is around 4:15/km. Without the HR clause a
    hot-weather threshold effort loses its last kilometres to the pace gate by
    a few seconds and gets logged as rest.
    """
    k = _gates(gates)
    return lp["d"] >= 180 and (
        lp["pace"] <= k.quality_lap_pace_sec_per_km
        or (lp["pw"] or 0) >= k.quality_lap_power_w
        or ((lp["aHR"] or 0) >= k.hr_rescue_min_bpm
            and lp["pace"] <= k.hr_rescue_pace_sec_per_km))


def extract_sets(plaps, gates=None):
    """Parsed laps (chronological) -> list of quality sets.

    rep  contiguous quality laps merged into one effort (sub-splits kept)
    set  consecutive reps of similar distance; the recovery seconds before each
         rep are kept so short-rest work can be told from full-rest work
    """
    k = _gates(gates)
    reps, rests = [], []            # rests[i] = recovery seconds before rep i
    cur, rest_s, seen = None, None, False
    for lp in plaps:
        if is_quality(lp, k):
            if cur is None:
                cur = {"d": 0, "s": 0, "subs": [], "aHR": [], "mHR": [], "pw": []}
                rests.append(rest_s if seen else None)
            cur["d"] += lp["d"]
            cur["s"] += lp["s"]
            cur["subs"].append(lp["s"])
            if (lp["aHR"] or 0) >= k.hr_dropout_below_bpm:
                cur["aHR"].append(lp["aHR"])
            if lp["mHR"]:
                cur["mHR"].append(lp["mHR"])
            if lp["pw"]:
                cur["pw"].append(lp["pw"])
            seen = True
        else:
            if cur is not None:
                reps.append(cur)
                cur = None
                rest_s = 0
            if seen:
                rest_s = (rest_s or 0) + lp["s"]
    if cur is not None:
        reps.append(cur)

    sets = []
    for rep, rest in zip(reps, rests):
        rep["pace"] = rep["s"] / (rep["d"] / 1000.0)
        if sets and abs(rep["d"] - sets[-1]["dmean"]) <= max(60, 0.18 * sets[-1]["dmean"]):
            st = sets[-1]
            st["reps"].append(rep)
            if rest:
                st["rests"].append(rest)
            st["dmean"] = sum(r["d"] for r in st["reps"]) / len(st["reps"])
        else:
            sets.append({"reps": [rep], "rests": [], "dmean": rep["d"]})
    return sets


def set_stats(st):
    """(best rep average HR, best rep maximum HR, mean rep power)."""
    reps = st["reps"]
    aHR = max((max(r["aHR"]) for r in reps if r["aHR"]), default=None)
    mHR = max((max(r["mHR"]) for r in reps if r["mHR"]), default=None)
    pws = [sum(r["pw"]) / len(r["pw"]) for r in reps if r["pw"]]
    pw = sum(pws) / len(pws) if pws else None
    return aHR, mHR, pw


def label_set(st, gates=None):
    """Name one set's physiological zone.

    Maximum lap HR is display-only and never classifies: one spike inside a
    threshold rep would promote the whole session.
    """
    k = _gates(gates)
    d, reps = st["dmean"], st["reps"]
    aHR, _, pw = set_stats(st)
    if d <= 250:
        return "Speed"
    if d <= 600:
        t = sum(r["s"] for r in reps) / len(reps)
        rest = sum(st["rests"]) / len(st["rests"]) if st["rests"] else None
        return "Lactate" if (rest is not None and rest <= 1.5 * t) else "Anaerobic"
    if len(reps) == 1 and d >= 2000:                      # one continuous effort
        if (aHR or 0) >= k.hr_obla_bpm:
            return "OBLA"
        if (aHR or 0) >= k.hr_threshold_bpm or (pw or 0) >= k.power_threshold_w:
            return "Threshold"
        return "Steady"
    if (pw or 0) >= k.power_vo2_w or (aHR or 0) >= k.hr_vo2_bpm:
        return "VO2"
    if (aHR or 0) >= k.hr_obla_bpm:
        return "OBLA"
    return "Threshold"


def _rep_dist(st):
    return int(round(st["dmean"] / 50) * 50)


def _set_menu(st):
    d = _rep_dist(st)
    return f"{d}m" if len(st["reps"]) == 1 else f"{len(st['reps'])}×{d}m"


def _set_line(st):
    """One data line per set: `1000m 3'36"-3'47"-3'48" HRmax165 347W`."""
    d, reps = _rep_dist(st), st["reps"]
    aHR, mHR, pw = set_stats(st)
    if len(reps) == 1 and st["dmean"] >= 2000:            # continuous: km splits
        r = reps[0]
        subs = f" ({'-'.join(fmt_rep(s) for s in r['subs'])})" if len(r["subs"]) > 1 else ""
        hr = f" aHR{int(aHR)}" if aHR else (f" HRmax{int(mHR)}" if mHR else "")
    else:
        subs = ""
        hr = f" HRmax{int(mHR)}" if mHR else ""
    splits = "-".join(fmt_rep(r["s"]) for r in reps)
    watts = f" {pw:.0f}W" if pw else ""
    return f"{d}m {splits}{subs}{hr}{watts}"


# --------------------------------------------------------------------------- #
# a day                                                                        #
# --------------------------------------------------------------------------- #
def build_entry(g, date_str, acts, gates=None, race=False, laps_of=None):
    """(kind, menu, data, is_workout) for one day.

    `race=True` comes from the Log's hand-written race column and forces the
    label, so a race is filed as a race rather than as whichever training zone
    its heart rate happened to resemble.

    `laps_of` is an injection point for tests: a callable (activity) -> laps,
    defaulting to reading them from Garmin.
    """
    k = _gates(gates)
    laps_of = laps_of or (lambda a: parse_laps(g, a))
    runs = [a for a in acts
            if typekey(a) in RUN_TYPES and (a.get("distance") or 0) >= 100]
    if not runs:
        return ("rest", "", "", False)
    runs = sorted(runs, key=lambda a: a["startTimeLocal"])

    sets_all, easy_runs, strides = [], [], 0
    for a in runs:
        pl = laps_of(a)
        strides += sum(1 for lp in pl
                       if lp["d"] < 180 and lp["pace"] <= k.stride_pace_sec_per_km)
        sets = extract_sets(pl, k) if any(is_quality(lp, k) for lp in pl) else []
        if sets:
            sets_all += sets
        else:
            easy_runs.append(a)

    easy_km = sum(a["distance"] for a in easy_runs) / 1000.0

    if not sets_all:                                      # plain jog day
        main = [a for a in easy_runs if a["distance"] >= 1000]
        small = sum(a["distance"] for a in easy_runs if a["distance"] < 1000) / 1000.0
        if not main:                                      # only tiny shuffles
            main, small = easy_runs, 0.0
        menu = " + ".join(f"{fmt_km(a['distance'])} {fmt_min(a['duration'])}"
                          for a in main)
        if small >= 0.3:
            menu += f" + jog計{small:.1f}km"
        data = " / ".join(
            f"{fmt_pace(a['duration'], a['distance'])} {fmt_hr(a.get('averageHR'))}".strip()
            for a in main)
        kind = "Jog + WS" if strides >= 2 else "Jog"
        return (kind, menu, data, False)

    # workout day: drop noise sets (a lone leftover surge, under 400 m and
    # under 15% of the biggest set) so the label stays honest
    def vol(st):
        return sum(r["d"] for r in st["reps"])

    vmax = max(vol(s) for s in sets_all)
    sets_all = [s for s in sets_all if vol(s) >= 400 or vol(s) >= 0.15 * vmax]

    labels = []
    for st in sets_all:
        lb = label_set(st, k)
        if not labels or labels[-1] != lb:
            labels.append(lb)
    if race:
        labels = ["race"]

    # the jog component goes on the side where the easy volume actually sits
    if easy_km >= 4.0:
        first_wo = next(a for a in runs if a not in easy_runs)
        pre = sum(a["distance"] for a in easy_runs
                  if a["startTimeLocal"] < first_wo["startTimeLocal"]) / 1000.0
        if pre >= easy_km / 2 and not race:
            labels.insert(0, "Jog")
        else:
            labels.append("Jog")
    kind = " + ".join(labels)

    menu = " + ".join(_set_menu(st) for st in sets_all)
    if easy_km >= 0.5:
        menu += f" + jog計{easy_km:.1f}km"

    lines = [_set_line(st) for st in sets_all]
    for a in easy_runs:                                   # steady legs >= 1.5 km
        spk = a["duration"] / (a["distance"] / 1000.0)
        if a["distance"] >= 1500 and spk <= 420:          # skip walking legs
            lines.append(f"{a['distance']/1000:.1f}km "
                         f"{fmt_pace(a['duration'], a['distance'])} "
                         f"{fmt_hr(a.get('averageHR'))}".strip())
    return (kind, menu, "\n".join(lines), True)
