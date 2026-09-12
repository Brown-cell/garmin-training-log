# -*- coding: utf-8 -*-
"""Everything that talks to Garmin Connect, and nothing that judges.

  fetch_day(g, d)             wellness fields the scores are computed from
  fetch_daily_activity(g, d)  movement and load for one day
  fetch_status_panel(g, d)    the slow-moving fitness snapshot
  run_km(g, d)                the day's running distance
  morning_readiness(g, d)     the ONE readiness snapshot that is comparable
                              across days

Every field degrades to None if Garmin returns nothing for that day (an
un-synced watch, or a rest day with no wearable data). None means "not known",
never "zero" -- the difference matters downstream, where a missing day would
otherwise be averaged in as a rest day.
"""
import datetime as dt

from . import classify, config


def _try(fn, *a):
    """Call and swallow. One bad endpoint must not lose the whole day."""
    try:
        return fn(*a)
    except Exception:
        return None


def _first_device(dmap):
    """Garmin nests per-device dicts under a dynamic device-id key; take one."""
    if isinstance(dmap, dict) and dmap:
        return next(iter(dmap.values()))
    return {}


# --------------------------------------------------------------------------- #
# readiness                                                                    #
# --------------------------------------------------------------------------- #
def pick_morning(tr):
    """Choose the wake-up snapshot out of a day's readiness snapshots.

    Garmin returns the day's snapshots newest first, so element 0 is whatever
    was last recomputed -- usually the post-exercise reset. A day with one
    snapshot then holds a morning value and a day with five holds an evening
    one, and putting both in the same column produces a series that cannot be
    compared across days at all. Only the after-wake-up reset means the same
    thing every day.

    Split out from the fetch so a caller holding raw snapshots can tell "the
    request failed" from "this day has no wake-up reset".
    """
    if isinstance(tr, dict):                    # some versions return a bare dict
        tr = [tr]
    tr = [e for e in (tr or []) if isinstance(e, dict)]
    wake = [e for e in tr if e.get("inputContext") == "AFTER_WAKEUP_RESET"]
    if wake:
        return min(wake, key=lambda e: e.get("timestampLocal") or "")
    if tr and all(e.get("inputContext") is None for e in tr):
        # devices that report no context: the earliest snapshot is the morning
        return min(tr, key=lambda e: e.get("timestampLocal") or "")
    return {}


def morning_readiness(g, date_str):
    """The wake-up readiness snapshot, or {} when the day has none.

    garminconnect's own get_morning_training_readiness() silently falls back to
    the newest snapshot on days without a wake-up reset, which reintroduces
    exactly the problem above, so it is not used. A day with no wake-up reset
    is left empty rather than filled with a number that means something else.
    """
    return pick_morning(_try(g.get_training_readiness, date_str) or [])


# --------------------------------------------------------------------------- #
# wellness                                                                     #
# --------------------------------------------------------------------------- #
def fetch_day(g, d, athlete=None):
    """Pull the wellness fields the scores need for one YYYY-MM-DD."""
    o = {"date": d}
    sleep = _try(g.get_sleep_data, d) or {}
    dto = sleep.get("dailySleepDTO") or {}
    o["sleep_s"] = dto.get("sleepTimeSeconds")
    o["deep_s"] = dto.get("deepSleepSeconds")
    o["rem_s"] = dto.get("remSleepSeconds")
    o["light_s"] = dto.get("lightSleepSeconds")
    o["awake_s"] = dto.get("awakeSleepSeconds")
    o["o_hrv"] = sleep.get("avgOvernightHrv")

    hrv = (_try(g.get_hrv_data, d) or {}).get("hrvSummary") or {}
    o["hrv"] = hrv.get("lastNightAvg")
    o["hrv_weekly"] = hrv.get("weeklyAvg")
    o["hrv_status"] = hrv.get("status")
    base = hrv.get("baseline") or {}
    o["b_low"] = base.get("balancedLow")        # bottom of the healthy band
    o["b_high"] = base.get("balancedUpper")
    o["low_up"] = base.get("lowUpper")          # below this Garmin says "low"

    stats = _try(g.get_stats, d) or {}
    o["rhr"] = stats.get("restingHeartRate")
    o["avg_stress"] = stats.get("averageStressLevel")
    o["rest_pct"] = stats.get("restStressPercentage")   # % of day at rest

    resp = _try(g.get_respiration_data, d) or {}
    o["resp"] = (resp.get("avgSleepRespirationValue")
                 or resp.get("avgWakingRespirationValue"))

    tr = morning_readiness(g, d)
    o["readiness"] = tr.get("score")
    o["stress_hist"] = tr.get("stressHistoryFactorPercent")
    # The wake-up snapshot carries several days of history folded into one
    # day's payload, which is what lets the condition score close over a single
    # day instead of walking backwards through the log.
    o["sleep_hist"] = tr.get("sleepHistoryFactorPercent")   # accumulated debt
    o["sleep_pct"] = tr.get("sleepScoreFactorPercent")      # last night
    o["hrv_factor"] = tr.get("hrvFactorPercent")
    o["acwr_pct"] = tr.get("acwrFactorPercent")             # load side, unused here
    o["rec_pct"] = tr.get("recoveryTimeFactorPercent")

    bb = _try(g.get_body_battery, d, d)
    o["bb_charged"] = bb[0].get("charged") if isinstance(bb, list) and bb else None

    o["load_explained"], o["load_reason"] = load_explanation(g, d, athlete=athlete)
    o["recent_load"] = o["load_explained"] >= 0.8
    return o


LOAD_SPORTS = {"running", "track_running", "trail_running", "treadmill_running",
               "virtual_run", "indoor_running", "cycling", "road_biking",
               "indoor_cycling", "mountain_biking", "gravel_cycling", "virtual_ride"}


def load_explanation(g, d, athlete=None):
    """How well recent training explains last night's HRV, 0..1, and why.

    0 means nothing recent can account for a drop; 1 means it is fully
    accounted for. Each day in the window gets a strength from its load, its
    distance and its anaerobic training effect (whichever is largest), scaled
    between the easy and hard bounds in athlete.json, then weighted by how long
    ago it was. The window takes the MAXIMUM, not the sum: three easy jogs do
    not add up to one hard day.

    Two failure modes are handled explicitly because both were silent before:
    non-running sports are excluded, so a bike ride or a hike cannot excuse a
    drop by accident; and when the request itself fails, no excuse is granted
    but the reason records that the load is unknown. Reading illness as fatigue
    costs more than reading fatigue as illness.
    """
    a = athlete or config.athlete()
    k = a.section("load_explanation")
    weights = {int(x): float(w) for x, w in (k.get("day_weights") or {}).items()}
    if not weights:
        raise config.ConfigError("'load_explanation.day_weights' is empty.")

    day = dt.date.fromisoformat(d)
    ex, ok, best = 0.0, True, (0, 0.0, 0.0)
    for back, weight in sorted(weights.items()):
        ds = (day - dt.timedelta(days=back)).isoformat()
        acts = _try(g.get_activities_by_date, ds, ds)
        if acts is None:
            ok = False
            continue
        dl = dkm = dan = 0.0
        for act in acts:
            if classify.typekey(act) not in LOAD_SPORTS:
                continue
            dl += act.get("activityTrainingLoad") or 0
            dkm += (act.get("distance") or 0) / 1000.0
            dan = max(dan, act.get("anaerobicTrainingEffect") or 0)
        s = min(1.0, max(
            (dl - k["easy_load"]) / (k["hard_load"] - k["easy_load"]),
            (dkm - k["easy_km"]) / (k["hard_km"] - k["easy_km"]),
            (dan - k["easy_anaerobic_te"])
            / (k["hard_anaerobic_te"] - k["easy_anaerobic_te"]),
            0.0))
        if weight * s > ex:
            ex, best = weight * s, (back, dl, dkm)
    if ex == 0.0:
        return 0.0, ("負荷不明(取得失敗・免罪なし)" if not ok else "直近に練習なし")
    return ex, (f"{best[0]}日前 負荷{best[1]:.0f}/{best[2]:.1f}km→説明{ex:.2f}"
                + ("" if ok else "(一部取得失敗)"))


# --------------------------------------------------------------------------- #
# activity and load                                                            #
# --------------------------------------------------------------------------- #
def run_km(g, d):
    """The day's cumulative running distance in km, from the activities.

    Read from Garmin rather than parsed out of the workout text: interval days
    rarely spell a total distance in their menu line, which used to leave the
    distance column empty on exactly the hardest days.
    """
    acts = _try(g.get_activities_by_date, d, d) or []
    total_m = sum((a.get("distance") or 0) for a in acts
                  if classify.typekey(a) in classify.RUN_TYPES
                  and (a.get("distance") or 0) >= 100)
    return round(total_m / 1000, 2) if total_m else ""


def fetch_daily_activity(g, d):
    """Per-day movement and load numbers."""
    o = {"date": d}
    stats = _try(g.get_stats, d) or {}
    o["steps"] = stats.get("totalSteps")
    fa = stats.get("floorsAscended")
    o["floors"] = round(fa) if fa is not None else None
    mod = stats.get("moderateIntensityMinutes") or 0
    vig = stats.get("vigorousIntensityMinutes") or 0
    o["intensity_min"] = mod + 2 * vig          # Garmin's own weighting
    o["active_kcal"] = stats.get("activeKilocalories")

    bb = _try(g.get_body_battery, d, d)
    if isinstance(bb, list) and bb:
        o["bb_charged"] = bb[0].get("charged")
        o["bb_drained"] = bb[0].get("drained")
    else:
        o["bb_charged"] = o["bb_drained"] = None

    ts = _try(g.get_training_status, d) or {}
    tsd = _first_device((ts.get("mostRecentTrainingStatus") or {})
                        .get("latestTrainingStatusData"))
    acwr = tsd.get("acuteTrainingLoadDTO") or {}
    o["acwr"] = acwr.get("dailyAcuteChronicWorkloadRatio")
    o["acwr_status"] = acwr.get("acwrStatus")
    o["load_acute"] = acwr.get("dailyTrainingLoadAcute")
    o["load_chronic"] = acwr.get("dailyTrainingLoadChronic")
    o["train_status"] = tsd.get("trainingStatusFeedbackPhrase")

    # The LAST snapshot of the day is the right one here, unlike readiness:
    # recovery time means "hours left before recovered", measured at the end of
    # the day, and that is comparable across days. Taken at the wake-up reset
    # it is near zero on every day including the day after a hard session.
    tr = _try(g.get_training_readiness, d) or []
    if isinstance(tr, list) and tr:
        rt = tr[0].get("recoveryTime")          # minutes
        o["recovery_h"] = round(rt / 60) if rt is not None else None
        o["stress_hist"] = tr[0].get("stressHistoryFactorPercent")
    else:
        o["recovery_h"] = o["stress_hist"] = None

    acts = _try(g.get_activities_by_date, d, d) or []
    load = sum((a.get("activityTrainingLoad") or 0) for a in acts) if acts else 0
    o["session_load"] = round(load) if load else None
    return o


def _sec_to_pace(sec_per_km):
    if not sec_per_km:
        return None
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def fetch_status_panel(g, d):
    """The slow-moving fitness snapshot behind the Status dashboard."""
    o = {"date": d}
    ts = _try(g.get_training_status, d) or {}
    vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic") or {})
    o["vo2max"] = vo2.get("vo2MaxValue")
    tsd = _first_device((ts.get("mostRecentTrainingStatus") or {})
                        .get("latestTrainingStatusData"))
    o["train_status"] = tsd.get("trainingStatusFeedbackPhrase")
    acwr = tsd.get("acuteTrainingLoadDTO") or {}
    o["acwr"] = acwr.get("dailyAcuteChronicWorkloadRatio")
    o["acwr_status"] = acwr.get("acwrStatus")

    lb = _first_device((ts.get("mostRecentTrainingLoadBalance") or {})
                       .get("metricsTrainingLoadBalanceDTOMap"))
    o["load_aero_low"] = round(lb.get("monthlyLoadAerobicLow") or 0)
    o["load_aero_high"] = round(lb.get("monthlyLoadAerobicHigh") or 0)
    o["load_anaerobic"] = round(lb.get("monthlyLoadAnaerobic") or 0)
    o["load_balance_fb"] = lb.get("trainingBalanceFeedbackPhrase")
    o["tgt_aero_low"] = (lb.get("monthlyLoadAerobicLowTargetMin"),
                         lb.get("monthlyLoadAerobicLowTargetMax"))
    o["tgt_aero_high"] = (lb.get("monthlyLoadAerobicHighTargetMin"),
                          lb.get("monthlyLoadAerobicHighTargetMax"))
    o["tgt_anaerobic"] = (lb.get("monthlyLoadAnaerobicTargetMin"),
                          lb.get("monthlyLoadAnaerobicTargetMax"))

    rp = _try(g.get_race_predictions) or {}
    o["race_5k"] = rp.get("time5K")
    o["race_10k"] = rp.get("time10K")

    lt = _try(g.get_lactate_threshold) or {}
    shr = lt.get("speed_and_heart_rate") or {}
    o["lt_hr"] = shr.get("heartRate")
    raw = shr.get("speed")                      # returned as (m/s)/10
    o["lt_speed_ms"] = round(raw * 10, 2) if raw else None
    o["lt_pace"] = _sec_to_pace(1000 / (raw * 10)) if raw else None
    return o


def last_sync_local(g):
    """Local datetime of the watch's last upload, or None if unavailable."""
    try:
        ms = (g.get_device_last_used() or {}).get("lastUsedDeviceUploadTime")
        return dt.datetime.fromtimestamp(ms / 1000) if ms else None
    except Exception:
        return None
