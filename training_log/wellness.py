# -*- coding: utf-8 -*-
"""Two derived scores that turn raw Garmin wellness into a judgement.

Both are pure functions of one day's fetched fields (see `garmin_fetch.fetch_day`),
so they can be recomputed in any order and tested without a network.

  sleep_score      0-100. Duration, gated hard, plus stage adequacy (deep and
                   REM as fractions of the night), continuity, and how far
                   overnight HRV recovered towards the personal band. A short
                   night cannot score well however good the stages are.

  condition_score  0-100. Is the body intact: illness, sleep debt, autonomic
                   disturbance. Starts at 100 and subtracts four
                   independent deductions: autonomic (HRV against the personal
                   band), sleep (last night plus accumulated debt), somatic
                   (resting HR, respiration, daytime rest), and secondary
                   (stress load, overnight recharge).

Two properties of the condition score are the design:

1. Recent training excuses only the autonomic term. A hard session does lower
   HRV, and that is productive fatigue rather than illness, so the autonomic
   deduction shrinks in proportion to how well recent load explains the drop.
   Training does not explain a short night, so the sleep terms are never
   forgiven. An earlier version forgave everything at once and could not leave
   its top label while the excuse gate was on.

2. The excuse is continuous rather than a switch. A boolean gate puts a cliff
   between "one jog forgives everything" and "nothing forgives anything", and
   which side of the cliff a day lands on moves the score by tens of points.

The score is NOT "how hard can I train today". Training fatigue is what the
fitness-fatigue model and Garmin's own readiness measure; the two disagree
most on the day after a hard session, and that is by design. Read them side by
side: this score for whether the body is healthy, TSB and readiness for
whether it is fresh.

Missing inputs are not bad inputs. A deduction-based score with no data reads
as a perfect day, so a day missing both main channels is not scored at all,
and a day missing a minor channel is marked with a trailing asterisk.
"""
from . import config

# Labels written into the sheet, kept in the sheet's own language.
#   好調 = strong / 良好 = fine, with a small deduction /
#   要観察 = watch this / 不調 = unwell
LABELS = ("好調", "良好", "要観察", "不調")
SLEEP_LABELS = ("優", "良", "可", "不足")      # excellent / good / fair / short


def params(athlete=None):
    """Condition-score constants from athlete.json's `condition_score`."""
    a = athlete or config.athlete()
    return a.section("condition_score")


def _lin(x, lo, hi):
    """0 at lo, 1 at hi, clamped."""
    if hi == lo:
        return 1.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def baselines(days):
    """Rolling personal baselines from a list of fetched days."""
    import statistics as st
    rhrs = [x["rhr"] for x in days if x.get("rhr")]
    resps = [x["resp"] for x in days if x.get("resp")]
    out = {}
    if rhrs:
        out["rhr"] = st.median(rhrs)
    if resps:
        out["resp"] = st.median(resps)
    return out


# --------------------------------------------------------------------------- #
# sleep                                                                        #
# --------------------------------------------------------------------------- #
def sleep_score(o):
    """(score, label, reason) for one night, or (None, 'no-data', '')."""
    s = o.get("sleep_s")
    if not s:
        return None, "no-data", ""
    h = s / 3600.0

    def frac(x):
        return (x or 0) / s

    if h >= 7.5:
        dur = 100 - max(0.0, (h - 9.0)) * 8               # mild penalty past 9 h
    elif h >= 6.5:
        dur = 80 + (h - 6.5) / 1.0 * 20
    elif h >= 5.5:
        dur = 55 + (h - 5.5) / 1.0 * 25
    else:
        dur = h / 5.5 * 55
    dur = max(0.0, min(100.0, dur))

    deep = _lin(frac(o.get("deep_s")), 0.05, 0.13) * 30
    rem = _lin(frac(o.get("rem_s")), 0.08, 0.18) * 30
    cont = (1 - _lin(frac(o.get("awake_s")), 0.03, 0.12)) * 20
    if o.get("o_hrv") and o.get("b_low"):
        ohrv = min(1.0, o["o_hrv"] / o["b_low"]) * 20
    else:
        ohrv = 14.0                                       # neutral when missing
    qual = deep + rem + cont + ohrv

    score = round(0.45 * dur + 0.55 * qual)
    label = (SLEEP_LABELS[0] if score >= 85 else
             SLEEP_LABELS[1] if score >= 70 else
             SLEEP_LABELS[2] if score >= 55 else SLEEP_LABELS[3])
    reason = (f"{h:.1f}h/深{frac(o.get('deep_s'))*100:.0f}%"
              f"/レム{frac(o.get('rem_s'))*100:.0f}%")
    return score, label, reason


# --------------------------------------------------------------------------- #
# condition                                                                    #
# --------------------------------------------------------------------------- #
def _hrv_dev(v, b_low, low_up):
    """How far below the personal band: 0 inside it, 0.5 at the bottom of the
    unbalanced zone, 1 when collapsed."""
    if v is None or b_low is None:
        return None
    if v >= b_low:
        return 0.0
    if v >= low_up:
        return _lin(b_low - v, 0, b_low - low_up) * 0.5
    return min(1.0, 0.5 + _lin(low_up - v, 0, low_up * 0.30) * 0.5)


def condition_score(o, base, k=None):
    """(0-100, label, reason). (None, 'no-data', why) when unscoreable."""
    k = k if k is not None else params()
    have_hrv = ((o.get("hrv") is not None or o.get("hrv_weekly") is not None)
                and o.get("b_low") is not None)
    have_sleep = bool(o.get("sleep_s")) or o.get("sleep_hist") is not None
    if not (have_hrv and have_sleep):
        lack = "・".join(x for x, ok in (("HRV", have_hrv), ("睡眠", have_sleep))
                         if not ok)
        return None, "no-data", f"{lack}が無いので採点しない"

    notes, missing, ded = [], [], {}
    score = 100.0
    b_low = o.get("b_low")
    low_up = o.get("low_up") or ((b_low - 4) if b_low else None)

    # ---- 1. autonomic ----------------------------------------------------- #
    # Last night and the weekly average are graded separately. Falling back to
    # the weekly average when last night is missing would silently raise the
    # score on exactly the days with the least evidence.
    dn = _hrv_dev(o.get("hrv"), b_low, low_up)
    dw = _hrv_dev(o.get("hrv_weekly"), b_low, low_up)
    if dn is None:
        dev = dw
        missing.append("前夜HRV")
    elif dw is None:
        dev = dn
        missing.append("HRV週平均")
    else:
        dev = k["hrv_w_night"] * dn + k["hrv_w_week"] * dw
    if dev:
        ex = o.get("load_explained")
        if ex is None:
            ex = 1.0 if o.get("recent_load") else 0.0
        w = k["hrv_pen_unexplained"] - (k["hrv_pen_unexplained"]
                                        - k["hrv_pen_explained"]) * ex
        p = dev * w
        score -= p
        ded["HRV"] = ded.get("HRV", 0) + p
        notes.append(f"HRV低下 -{p:.0f}(練習による説明 {ex:.0%})")

    # ---- 2. sleep (never excused by training) ----------------------------- #
    ss = sleep_score(o)[0]
    if ss is None:
        missing.append("睡眠")
    else:
        p = min(k["sleep_cap"], max(0.0, k["sleep_pivot"] - ss) * k["sleep_k"])
        if p >= 0.5:
            score -= p
            ded["睡眠"] = ded.get("睡眠", 0) + p
            notes.append(f"前夜の睡眠({ss}) -{p:.0f}")
    sh = o.get("sleep_hist")
    if sh is None:
        missing.append("睡眠履歴")
    else:
        p = min(k["debt_cap"], max(0.0, k["debt_pivot"] - sh) * k["debt_k"])
        if p >= 0.5:
            score -= p
            ded["睡眠"] = ded.get("睡眠", 0) + p
            notes.append(f"睡眠不足の蓄積({sh}%) -{p:.0f}")

    # ---- 3. somatic (also never excused) ---------------------------------- #
    if not o.get("rhr"):
        missing.append("RHR")
    else:
        d = o["rhr"] - base["rhr"]
        p = min(k["rhr_cap"], max(0.0, (d - k["rhr_dead"]) * k["rhr_k"]))
        if p >= 0.5:
            score -= p
            ded["RHR"] = p
            notes.append(f"RHR+{d:.0f} -{p:.0f}")
    if not o.get("resp"):
        missing.append("呼吸")
    else:
        d = o["resp"] - base["resp"]
        p = min(k["resp_cap"], max(0.0, (d - k["resp_dead"]) * k["resp_k"]))
        if p >= 0.5:
            score -= p
            ded["呼吸"] = p
            notes.append(f"呼吸+{d:.1f} -{p:.0f}")
    # rest_pct and bb_charged come back as 0 when absent, and a real day is
    # always positive, so 0 means missing rather than terrible.
    rp = o.get("rest_pct")
    if not rp:
        missing.append("安静回復")
    elif rp < k["rest_pivot"]:
        p = min(k["rest_cap"], (k["rest_pivot"] - rp) * k["rest_k"])
        score -= p
        ded["安静回復"] = p
        notes.append(f"安静回復乏({rp:.0f}%) -{p:.0f}")

    # ---- 4. secondary (small, capped) ------------------------------------- #
    sth = o.get("stress_hist")
    if sth is not None and sth < k["stress_pivot"]:
        p = min(k["stress_cap"], (k["stress_pivot"] - sth) * k["stress_k"])
        score -= p
        ded["ストレス"] = p
        notes.append(f"ストレス負荷({sth}) -{p:.0f}")
    bbc = o.get("bb_charged")
    if bbc and bbc < k["bb_pivot"] and (o.get("load_explained") or 0) < 0.5:
        p = min(k["bb_cap"], (k["bb_pivot"] - bbc) * k["bb_k"])
        score -= p
        ded["夜間回復"] = p
        notes.append(f"夜間回復不良({bbc}) -{p:.0f}")

    score = int(round(max(0.0, min(100.0, score))))
    # The middle label says "fine", not "tired": the deduction is not
    # necessarily fatigue, and calling a short night fatigue would be a lie.
    # What it actually was is named in the bracket.
    label = (LABELS[0] if score >= k["lb_good"] else
             LABELS[1] if score >= k["lb_fatigue"] else
             LABELS[2] if score >= k["lb_watch"] else LABELS[3])
    if ded:
        name, val = max(ded.items(), key=lambda kv: kv[1])
        if val >= 5:
            label += f"({name}-{val:.0f})"
    if missing:
        label += "*"                          # a minor input was unavailable
        notes.append("欠測:" + "・".join(missing))
    if o.get("load_reason"):
        notes.append(o["load_reason"])
    return score, label, "・".join(notes) or "クリーン"
