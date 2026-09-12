# -*- coding: utf-8 -*-
"""Run the session classifier on the lap fixtures from tests/test_classify.py.

No network, no credentials, no spreadsheet: the laps are built here and the
thresholds come from athlete.example.json. The output block at the top of the
README is this script's stdout.

    python examples/classify_demo.py
"""
from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training_log import classify, config  # noqa: E402


def lap(d, s, aHR=None, mHR=None, pw=None):
    return {"d": float(d), "s": float(s), "pace": s / (d / 1000.0),
            "aHR": aHR, "mHR": mHR, "pw": pw}


def activity(aid, start, distance, duration, aHR=140, kind="running"):
    return {"activityId": aid, "startTimeLocal": start, "distance": float(distance),
            "duration": float(duration), "averageHR": aHR,
            "activityType": {"typeKey": kind}}


def repeats(n, d, s, aHR, mHR, pw, rec_d, rec_s, rec_HR):
    laps = []
    for _ in range(n):
        laps.append(lap(d, s, aHR=aHR, mHR=mHR, pw=pw))
        laps.append(lap(rec_d, rec_s, aHR=rec_HR))
    return laps


CASES = [
    ("a rest day (nothing in Garmin)", [], {}, False),
    ("8 km at 5:00/km, HR 140",
     [activity(1, "2026-07-01 07:00:00", 8000, 2400)],
     {1: [lap(1000, 300, aHR=140) for _ in range(8)]}, False),
    ("8 km continuous at 4:00/km, HR 176, 340 W",
     [activity(1, "2026-07-01 18:00:00", 8000, 1920, aHR=176)],
     {1: [lap(1000, 240, aHR=176, mHR=181, pw=340) for _ in range(8)]}, False),
    ("5x1000m at 3:20/km, HR 186, 370 W, 200 m jog recovery",
     [activity(1, "2026-07-01 18:00:00", 6000, 1450, aHR=175)],
     {1: repeats(5, 1000, 200, 186, 190, 370, 200, 90, 150)}, False),
    ("8x200m at 2:30/km, HR 170, walk recovery",
     [activity(1, "2026-07-01 18:00:00", 3200, 1840, aHR=160)],
     {1: repeats(8, 200, 30, 170, 185, None, 200, 200, 140)}, False),
    ("a morning jog and an evening threshold run",
     [activity(1, "2026-07-01 07:00:00", 6000, 1800),
      activity(2, "2026-07-01 18:00:00", 8000, 1920, aHR=176)],
     {1: [lap(1000, 300, aHR=138) for _ in range(6)],
      2: [lap(1000, 240, aHR=176, mHR=181, pw=340) for _ in range(8)]}, False),
]


def main() -> int:
    athlete = config.load_athlete(str(ROOT / "athlete.example.json"))
    gates = classify.Gates.from_athlete(athlete)

    print("Session classifier on the fixtures from tests/test_classify.py")
    print("(thresholds from athlete.example.json; no network, no credentials)")

    for description, acts, laps, race in CASES:
        kind, menu, data, workout = classify.build_entry(
            None, "2026-07-01", acts, gates=gates, race=race,
            laps_of=lambda a: laps.get(a["activityId"], []))
        lines = (data or "-").split("\n")
        print()
        print(f"  {'in':<8}{description}")
        print(f"  {'kind':<8}{(kind or '-'):<16}"
              f"quality session: {'yes' if workout else 'no'}")
        print(f"  {'menu':<8}{menu or '-'}")
        print(f"  {'result':<8}{lines[0]}")
        for extra in lines[1:]:
            print(f"  {'':<8}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
