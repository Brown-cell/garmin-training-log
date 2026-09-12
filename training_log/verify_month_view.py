# -*- coding: utf-8 -*-
"""Check a month-view tab that month_view.py built.

Prints PASS/FAIL per item and exits 1 if anything failed. The checks look at
the EVALUATED grid, i.e. what the spreadsheet actually shows, which is the only
way to catch a formula that is syntactically fine and returns an error.

  1 the tab exists and has one row per day of the month
  2 no #ERROR!/#REF!/#N/A anywhere
  3 the KPI row has a numeric monthly distance and quality-session count
  4 quality days show a multi-line content cell whenever Log has both a menu
    and a result for that day
  5 the five conditional-format rules are present

  python -m training_log.verify_month_view --month 7 --tab Jul
"""
import argparse
import calendar
import sys

from . import config, log_io

EASY = {"", "jog", "off", "rest"}
EXPECTED_RULES = 5          # 3 condition bands + 1 ACWR + 1 quality label


def build_parser():
    import datetime as dt
    today = dt.date.today()
    ap = argparse.ArgumentParser(
        prog="python -m training_log.verify_month_view",
        description="Verify a month-view tab renders without errors.")
    ap.add_argument("--year", type=int, default=today.year)
    ap.add_argument("--month", type=int, default=today.month)
    ap.add_argument("--tab", type=str, default=None,
                    help="tab name (default: the month's abbreviation)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    year, month = args.year, args.month
    tab = args.tab or calendar.month_abbr[month]
    fails = []

    def check(name, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            fails.append(name)

    sh = config.spreadsheet()
    print(f"verify '{tab}' ({year}-{month:02d})")
    try:
        ws = sh.worksheet(tab)
    except Exception:
        check("tab exists", False)
        return 1
    grid = ws.get_all_values()

    ndays = calendar.monthrange(year, month)[1]
    day_rows = [r for r in grid if r and r[0].startswith(f"{month}/")]
    check("day-row count", len(day_rows) == ndays, f"{len(day_rows)}/{ndays}")

    bad = [(i + 1, j + 1, c) for i, r in enumerate(grid) for j, c in enumerate(r)
           if isinstance(c, str) and c.startswith(("#ERROR", "#REF", "#N/A"))]
    check("no formula errors", not bad, f"first: {bad[:3]}" if bad else "")

    kpi = grid[2] if len(grid) > 2 else []
    try:
        km = float(kpi[1])
    except Exception:
        km = -1
    check("KPI monthly distance", km >= 0,
          f"km={kpi[1] if len(kpi) > 1 else '?'}")
    check("KPI quality count", len(kpi) > 5 and str(kpi[5]).isdigit(),
          f"n={kpi[5] if len(kpi) > 5 else '?'}")

    log = {r[0]: r for r in sh.worksheet(log_io.TAB).get_all_values()[1:]
           if r and r[0]}
    detail_i = log_io.HEADER.index("menu")
    data_i = log_io.HEADER.index("result")
    miss, nq = [], 0
    for r in day_rows:
        day = int(r[0].split("/")[1].split(" ")[0])
        kind = (r[2] or "").strip().lower()
        if kind in EASY:
            continue
        nq += 1
        lr = log.get(f"{year}-{month:02d}-{day:02d}", [])
        if (len(lr) > data_i and lr[detail_i].strip() and lr[data_i].strip()
                and "\n" not in r[4]):
            miss.append(r[0])
    check("quality content is multi-line", not miss,
          f"{nq} quality days" + (f", flat: {miss}" if miss else ""))

    meta = sh.fetch_sheet_metadata(
        {"fields": "sheets(properties.sheetId,conditionalFormats)"})
    ncf = next((len(s.get("conditionalFormats", [])) for s in meta["sheets"]
                if s["properties"]["sheetId"] == ws.id), 0)
    check(f"conditional-format rules == {EXPECTED_RULES}", ncf == EXPECTED_RULES,
          f"found {ncf}")

    print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL -> {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
