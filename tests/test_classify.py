# -*- coding: utf-8 -*-
"""The run classifier, on synthetic lap data.

Every case is built by hand so the expected label follows from the gates in
athlete.example.json rather than from whatever a real day happened to contain.
"""
import pathlib

from training_log import classify

# The placeholder label this project refuses to emit, spelled in pieces so that
# searching the repository for it finds nothing at all.
BANNED_LABEL = "Workout" + "?"


def lap(d, s, aHR=None, mHR=None, pw=None):
    return {"d": float(d), "s": float(s), "pace": s / (d / 1000.0),
            "aHR": aHR, "mHR": mHR, "pw": pw}


def activity(aid, start, distance, duration, aHR=140, kind="running"):
    return {"activityId": aid, "startTimeLocal": start, "distance": float(distance),
            "duration": float(duration), "averageHR": aHR,
            "activityType": {"typeKey": kind}}


def run(acts, laps, gates, race=False):
    """build_entry with the laps injected instead of fetched."""
    return classify.build_entry(None, "2026-07-01", acts, gates=gates, race=race,
                                laps_of=lambda a: laps.get(a["activityId"], []))


# --------------------------------------------------------------------------- #
def test_no_running_activity_is_rest(gates):
    assert run([], {}, gates) == ("rest", "", "", False)


def test_a_walk_is_not_a_run(gates):
    acts = [activity(1, "2026-07-01 07:00:00", 4000, 3000, kind="walking")]
    kind, _, _, workout = run(acts, {1: []}, gates)
    assert kind == "rest"
    assert workout is False


def test_easy_running_is_a_jog(gates):
    acts = [activity(1, "2026-07-01 07:00:00", 8000, 2400)]
    laps = {1: [lap(1000, 300, aHR=140) for _ in range(8)]}
    kind, menu, data, workout = run(acts, laps, gates)
    assert kind == "Jog"
    assert workout is False
    assert "8.00km" in menu
    assert "5:00/km" in data


def test_two_strides_after_a_jog_are_named(gates):
    acts = [activity(1, "2026-07-01 07:00:00", 8300, 2450)]
    laps = {1: [lap(1000, 300, aHR=140) for _ in range(8)]
               + [lap(120, 22, aHR=150) for _ in range(2)]}
    kind, _, _, workout = run(acts, laps, gates)
    assert kind == "Jog + WS"
    assert workout is False


def test_one_continuous_hard_effort_is_a_threshold_run(gates):
    acts = [activity(1, "2026-07-01 18:00:00", 8000, 1920, aHR=176)]
    laps = {1: [lap(1000, 240, aHR=176, mHR=181, pw=340) for _ in range(8)]}
    kind, menu, data, workout = run(acts, laps, gates)
    assert kind == "Threshold"
    assert workout is True
    assert menu.startswith("8000m")
    assert "aHR176" in data


def test_repeats_at_vo2_heart_rate_are_graded_vo2(gates):
    laps = []
    for _ in range(5):
        laps.append(lap(1000, 200, aHR=186, mHR=190, pw=370))
        laps.append(lap(200, 90, aHR=150))            # recovery
    acts = [activity(1, "2026-07-01 18:00:00", 6000, 1450, aHR=175)]
    kind, menu, _, workout = run(acts, {1: laps}, gates)
    assert kind == "VO2"
    assert workout is True
    assert "5×1000m" in menu


def test_short_fast_repeats_are_speed(gates):
    laps = []
    for _ in range(8):
        laps.append(lap(200, 30, aHR=170, mHR=185))
        laps.append(lap(200, 200, aHR=140))           # walk recovery
    acts = [activity(1, "2026-07-01 18:00:00", 3200, 1840, aHR=160)]
    kind, menu, _, _ = run(acts, {1: laps}, gates)
    assert kind == "Speed"
    assert "8×200m" in menu


def test_a_morning_jog_before_a_workout_is_kept_in_the_label(gates):
    jog = activity(1, "2026-07-01 07:00:00", 6000, 1800)
    work = activity(2, "2026-07-01 18:00:00", 8000, 1920, aHR=176)
    laps = {1: [lap(1000, 300, aHR=138) for _ in range(6)],
            2: [lap(1000, 240, aHR=176, mHR=181, pw=340) for _ in range(8)]}
    kind, menu, _, _ = run([jog, work], laps, gates)
    assert kind == "Jog + Threshold"
    assert "jog計6.0km" in menu


def test_the_race_column_overrides_the_zone(gates):
    acts = [activity(1, "2026-07-01 14:00:00", 3000, 570, aHR=185)]
    laps = {1: [lap(3000, 570, aHR=185, mHR=192, pw=400)]}
    kind, _, _, workout = run(acts, laps, gates, race=True)
    assert kind == "race"
    assert workout is True


def test_recovery_laps_split_the_reps(gates):
    """Two reps with a recovery between them are two reps, not one long one."""
    laps = [lap(1000, 200, aHR=186, pw=370), lap(300, 150, aHR=150),
            lap(1000, 202, aHR=186, pw=368)]
    sets = classify.extract_sets(laps, gates)
    assert len(sets) == 1
    assert len(sets[0]["reps"]) == 2
    assert sets[0]["rests"] == [150.0]


def test_auto_laps_inside_one_effort_are_merged(gates):
    """A watch chopping one continuous effort into pieces must not read as reps."""
    laps = [lap(1000, 240, aHR=176, pw=340) for _ in range(4)]
    sets = classify.extract_sets(laps, gates)
    assert len(sets) == 1
    assert len(sets[0]["reps"]) == 1
    assert sets[0]["reps"][0]["d"] == 4000


def test_power_rescues_a_lap_whose_heart_rate_dropped_out(gates):
    """Wrist HR reading 86 bpm on a fast rep must not demote the lap."""
    dropped = lap(1000, 200, aHR=86, pw=380)
    assert classify.is_quality(dropped, gates) is True
    sets = classify.extract_sets([dropped], gates)
    assert sets[0]["reps"][0]["aHR"] == []        # the bad reading is discarded


def test_the_placeholder_label_is_not_reachable(gates):
    """No input produces it, and the package does not contain the string."""
    cases = [
        ([], {}),
        ([activity(1, "2026-07-01 07:00:00", 8000, 2400)],
         {1: [lap(1000, 300, aHR=140) for _ in range(8)]}),
        ([activity(1, "2026-07-01 18:00:00", 8000, 1920, aHR=176)],
         {1: [lap(1000, 240, aHR=176, pw=340) for _ in range(8)]}),
        ([activity(1, "2026-07-01 18:00:00", 2000, 600, aHR=150)],
         {1: []}),
    ]
    for acts, laps in cases:
        assert BANNED_LABEL not in run(acts, laps, gates)[0]

    pkg = pathlib.Path(classify.__file__).parent
    for path in pkg.glob("*.py"):
        assert BANNED_LABEL not in path.read_text(encoding="utf-8"), path.name
