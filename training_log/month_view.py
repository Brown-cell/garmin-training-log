# -*- coding: utf-8 -*-
"""Build the human-facing monthly journal tab on top of Log.

`Log` is the database and nobody opens it. This tab is the surface: one row per
day, a week subtotal after every Sunday, a KPI strip at the top, and two hand
columns the athlete writes in.

Design decisions behind it:

  * Every cell except the hand columns is a formula reading Log, so the nightly
    refresh keeps this tab live without rebuilding it.
  * One hue, and no cell shading. A single-hue ramp only supports a few
    distinguishable lightness steps, and shading every number turns the month
    into wallpaper. Anomalies get bold navy text instead, so the few marks that
    appear actually mean something.
  * Only decision columns are marked: sleep, condition, ACWR, quality sessions.
    Raw physiology lives in a collapsed column group.
  * HRV gets an arrow only when it moves beyond one standard deviation of its
    own preceding 28 days, after Buchheit's smallest-worthwhile-change
    argument. An arrow on every wobble is the same as no arrow at all.
  * The arrow is a statistical mark rather than a health verdict. The condition
    score judges HRV against Garmin's personal band, a different threshold, so
    a day can lose condition points with no arrow.

Creating a tab is a different act from updating one and needs `--allow-new-tab`.
A missing tab is nearly always a mistyped name, silently creating one splits the
hand columns across a real tab and a decoy nobody opens, and the plan column has
no other copy anywhere.

  python -m training_log.month_view --month 7            # preview
  python -m training_log.month_view --month 7 --write
  python -m training_log.month_view --month 7 --tab Jul --write
"""
import argparse
import calendar
import datetime as dt
import sys

from . import classify, config, log_io

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def rgb(hexs):
    return {"red": int(hexs[0:2], 16) / 255, "green": int(hexs[2:4], 16) / 255,
            "blue": int(hexs[4:6], 16) / 255}


B1, B4, NAVY = rgb("E8F0FE"), rgb("3D85C6"), rgb("0B5394")
BORDER, WHITE = rgb("D5E3F7"), rgb("FFFFFF")
AMBER, RED = rgb("9A6700"), rgb("A61C00")

# A quality session is a label containing a zone word. Composites such as
# "Jog + Threshold" count; "Jog + WS", "Steady" and rest do not.
QUALITY_RE = ('"(?i)(vo2|obla|thresh|anaerob|lactat|speed|race|sharp|fartlek)"')

HEADER = ["date", "plan", "kind", "km", "detail", "sleep", "condition", "HRV",
          "ACWR", "load", "note", "sleep_h", "deep_h", "RHR", "readiness",
          "steps", "intensity", "BB", "recovery_h", ""]
NCOL = len(HEADER)                    # 20; the last column is a hidden helper
HAND = {"plan": 1, "note": 10}        # 0-based indexes of the hand columns


def log_col(name):
    """A1 letter of a Log column, derived from the schema rather than typed."""
    return log_io.colA1(log_io.HEADER.index(name))


# --------------------------------------------------------------------------- #
# formulas                                                                     #
# --------------------------------------------------------------------------- #
def d8(year, month, day):
    return f"DATE({year},{month},{day})"


def idx(col, r):
    """INDEX into a Log column, blank-safe (INDEX of an empty cell gives 0)."""
    c = f"Log!${col}:${col}"
    return f'IFERROR(IF(INDEX({c},$T{r})="","",INDEX({c},$T{r})),"")'


def hrv_formula(r, year, month, day):
    """HRV plus an arrow when it leaves one 28-day standard deviation."""
    col = log_col("HRV")
    c, a = f"Log!${col}$2:${col}$400", "Log!$A$2:$A$400"
    v = f'IF(INDEX(Log!${col}:${col},$T{r})="","",INDEX(Log!${col}:${col},$T{r}))'
    day8 = d8(year, month, day)
    f = f'FILTER({c},{a}>={day8}-28,{a}<{day8},{c}<>"")'
    return (f'=IFERROR(LET(v,{v},f,{f},m,IFERROR(AVERAGE(f),v),s,IFERROR(STDEV(f),0),'
            f'IF(v="","",v&IFS(v<m-s," ↓",v>m+s," ↑",TRUE,""))),"")')


def week_avg(col, year, month, dlo, dhi):
    c, a = f"Log!${col}$2:${col}$400", "Log!$A$2:$A$400"
    return (f'=IFERROR(ROUND(AVERAGE(FILTER({c},{a}>={d8(year, month, dlo)},'
            f'{a}<={d8(year, month, dhi)},{c}<>"")),0),"")')


def build_rows(year, month, gates):
    """(grid, day rows, subtotal rows). Rows are 1-based sheet rows."""
    km_c, cond_c, kind_c = log_col("km"), log_col("cond_score"), log_col("kind")
    mon = calendar.month_abbr[month]
    grid = [[""] * NCOL for _ in range(3)]     # title / KPI labels / KPI values
    grid[0][1] = f"Training log {year}-{month:02d}"   # column A is frozen
    lab = grid[1]
    lab[1], lab[2], lab[4] = "month km", "this week km", f"km by day ({mon})"
    lab[5], lab[6], lab[8], lab[10] = ("quality sessions", "mean condition",
                                       "ACWR", f"condition by day ({mon})")
    a, e = "Log!$A:$A", f"Log!${km_c}:${km_c}"
    lo, hi = d8(year, month, 1), d8(year, month, calendar.monthrange(year, month)[1])
    val = grid[2]
    val[1] = f'=ROUND(SUMIFS({e},{a},">="&{lo},{a},"<="&{hi}),1)'
    if (year, month) == (dt.date.today().year, dt.date.today().month):
        val[2] = (f'=ROUND(SUMIFS({e},{a},">="&TODAY()-WEEKDAY(TODAY(),3),'
                  f'{a},"<="&TODAY()),1)')
    else:
        grid[1][2] = ""                        # meaningless for an archive month
    val[4] = (f'=IFERROR(SPARKLINE(FILTER(Log!${km_c}$2:${km_c}$400,'
              f'Log!$A$2:$A$400>={lo},Log!$A$2:$A$400<={hi}),'
              f'{{"charttype","column";"color","#3D85C6";"ymin",0}}),"")')
    val[5] = (f'=SUMPRODUCT(--REGEXMATCH(IFERROR(FILTER(Log!${kind_c}$2:${kind_c}$400,'
              f'Log!$A$2:$A$400>={lo},Log!$A$2:$A$400<={hi}),""),{QUALITY_RE}))')
    val[6] = (f'=IFERROR(ROUND(AVERAGEIFS(Log!${cond_c}:${cond_c},{a},">="&{lo},'
              f'{a},"<="&{hi}),0),"")')
    acwr_c = log_col("ACWR")
    val[8] = (f'=IFERROR(ROUND(LOOKUP(2,1/((Log!${acwr_c}$2:${acwr_c}$400<>"")*'
              f'(Log!$A$2:$A$400<=TODAY())*(Log!$A$2:$A$400<={hi})),'
              f'Log!${acwr_c}$2:${acwr_c}$400),2),"")')
    val[10] = (f'=IFERROR(SPARKLINE(FILTER(Log!${cond_c}$2:${cond_c}$400,'
               f'Log!$A$2:$A$400>={lo},Log!$A$2:$A$400<={hi},'
               f'Log!$A$2:$A$400<=TODAY(),Log!${cond_c}$2:${cond_c}$400<>""),'
               f'{{"charttype","line";"color","#0B5394";"linewidth",2;'
               f'"ymin",40;"ymax",100}}),"")')
    grid.append(list(HEADER))

    day_rows, sub_rows, week = [], [], []
    nweek = 0
    ndays = calendar.monthrange(year, month)[1]
    detail_c, data_c = log_col("menu"), log_col("result")
    for day in range(1, ndays + 1):
        r = len(grid) + 1                      # 1-based sheet row
        date = dt.date(year, month, day)
        row = [""] * NCOL
        row[0] = f"{month}/{day} {WD[date.weekday()]}"
        row[2] = "=" + idx(kind_c, r)
        row[3] = "=" + idx(km_c, r)
        # detail = menu + result. On a quality day the result lines are stacked
        # under the menu line so every split is visible (the column wraps);
        # easy days stay on one line.
        row[4] = (f'=IFERROR(LET(d,IF(INDEX(Log!${detail_c}:${detail_c},$T{r})="","",'
                  f'INDEX(Log!${detail_c}:${detail_c},$T{r})),'
                  f'x,IF(INDEX(Log!${data_c}:${data_c},$T{r})="","",'
                  f'INDEX(Log!${data_c}:${data_c},$T{r})),'
                  f'q,AND($C{r}<>"",LOWER($C{r})<>"jog",LOWER($C{r})<>"off",'
                  f'LOWER($C{r})<>"rest"),'
                  f'IF(x="",d,IF(d="",x,d&IF(q,CHAR(10),"  ")&x))),"")')
        row[5] = "=" + idx(log_col("sleep_score"), r)
        row[6] = "=" + idx(cond_c, r)
        row[7] = hrv_formula(r, year, month, day)
        row[8] = "=" + idx(acwr_c, r)
        row[9] = "=" + idx(log_col("load"), r)
        for i, k in enumerate(["sleep_h", "deep_h", "RHR", "readiness", "steps",
                               "intensity_min", "bb_drained", "recovery_h"],
                              start=11):
            row[i] = "=" + idx(log_col(k), r)
        row[19] = f'=IFERROR(MATCH({d8(year, month, day)},Log!$A:$A,0),"")'
        grid.append(row)
        day_rows.append(r)
        week.append((day, r))
        if date.weekday() == 6 or day == ndays:        # close on Sunday / EOM
            nweek += 1
            r2 = len(grid) + 1
            sub = [""] * NCOL
            sub[0] = f"Week {nweek} total"
            sub[3] = f"=ROUND(SUM(D{week[0][1]}:D{week[-1][1]}),1)"
            sub[6] = week_avg(cond_c, year, month, week[0][0], week[-1][0])
            sub[9] = f"=SUM(J{week[0][1]}:J{week[-1][1]})"
            grid.append(sub)
            sub_rows.append(r2)
            week = []

    grid.append([""] * NCOL)
    for txt in ("condition: 90+ strong / 66-89 fine, small deduction (bold) / "
                "50-65 watch (amber) / below 50 unwell (red)",
                "load = Garmin activityTrainingLoad (derived from EPOC, "
                "unitless). For a weekly total, read the target band on the "
                "Status tab (CTL x 7).",
                "kind in bold navy = a quality session (VO2 / OBLA / Threshold "
                "/ Anaerobic / Lactate / Speed / race). detail = the menu line "
                "plus one result line per set (splits, HRmax, running watts)."):
        f = [""] * NCOL
        f[0] = txt
        grid.append(f)
    return grid, day_rows, sub_rows


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


def boolrule(sid, ranges, cond, fmt):
    return {"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": ranges, "booleanRule": {"condition": cond, "format": fmt}}}}


def note(sid, r, c, text):
    return {"updateCells": {"start": {"sheetId": sid, "rowIndex": r,
                                      "columnIndex": c},
                            "rows": [{"values": [{"note": text}]}],
                            "fields": "note"}}


def classifier_note(gates, acwr_lo, acwr_hi):
    """The hover note on `kind`, built from the athlete's own thresholds."""
    return ("kind is classified automatically from the lap structure (rep "
            "length, recovery), the average heart rate of the reps "
            f"(LT {gates.hr_threshold_bpm:.0f} / OBLA {gates.hr_obla_bpm:.0f} / "
            f"VO2 {gates.hr_vo2_bpm:.0f}+) and running power, which rescues a "
            "rep whose wrist heart rate dropped out "
            f"(Thr {gates.power_threshold_w:.0f} W / "
            f"VO2 {gates.power_vo2_w:.0f} W+). The thresholds live in "
            "athlete.json. If a label is wrong, type over the cell: the "
            "pipeline never overwrites it again.")


def fmt_requests(sid, nrow, sub_rows, ntotal, gates, acwr_lo, acwr_hi):
    q = []
    q.append({"updateSheetProperties": {"properties": {
        "sheetId": sid, "tabColor": B4,
        "gridProperties": {"frozenRowCount": 4, "frozenColumnCount": 1,
                           "hideGridlines": True}},
        "fields": "tabColor,gridProperties(frozenRowCount,frozenColumnCount,"
                  "hideGridlines)"}})
    q.append({"mergeCells": {"range": gr(sid, 0, 1, 1, NCOL),
                             "mergeType": "MERGE_ALL"}})
    q.append(repeat(sid, 0, 1, 0, NCOL, {
        "backgroundColor": B4, "verticalAlignment": "MIDDLE",
        "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 12}}))
    q.append(repeat(sid, 1, 2, 0, NCOL, {
        "textFormat": {"foregroundColor": NAVY, "fontSize": 10}}))
    q.append(repeat(sid, 2, 3, 0, NCOL, {
        "textFormat": {"foregroundColor": NAVY, "bold": True, "fontSize": 10}}))
    q.append(repeat(sid, 3, 4, 0, NCOL, {
        "backgroundColor": B4, "wrapStrategy": "CLIP",
        "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 10}}))
    # Data rows clip, except the label and content columns which wrap so a
    # quality day shows its full splits. Numbers stay top-aligned with the menu
    # line so a multi-line row still scans horizontally.
    q.append(repeat(sid, 4, nrow, 0, NCOL,
                    {"wrapStrategy": "CLIP", "verticalAlignment": "TOP"}))
    q.append(repeat(sid, 4, nrow, 2, 3, {"wrapStrategy": "WRAP"}))
    q.append(repeat(sid, 4, nrow, 4, 5, {"wrapStrategy": "WRAP"}))
    for r in sub_rows:
        q.append(repeat(sid, r - 1, r, 0, NCOL, {
            "backgroundColor": B1,
            "textFormat": {"foregroundColor": NAVY, "bold": True, "fontSize": 10}}))
    q.append(repeat(sid, 4, nrow, 5, 7,
                    {"numberFormat": {"type": "NUMBER", "pattern": "0"}}))
    q.append(repeat(sid, 4, nrow, 3, 4,
                    {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
    q.append(repeat(sid, 4, nrow, 8, 9,
                    {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}))
    q.append(repeat(sid, 4, nrow, 11, 13,
                    {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
    widths = [64, 120, 92, 46, 300, 58, 58, 64, 52, 46, 260,
              44, 40, 44, 44, 56, 48, 40, 44, 20]
    for i, w in enumerate(widths):
        q.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    q.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": 19, "endIndex": 20},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    q.append({"addDimensionGroup": {"range": {
        "sheetId": sid, "dimension": "COLUMNS", "startIndex": 11, "endIndex": 19}}})
    q.append({"updateDimensionGroup": {"dimensionGroup": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": 11, "endIndex": 19},
        "depth": 1, "collapsed": True}, "fields": "collapsed"}})
    q.append({"updateBorders": {"range": gr(sid, 4, nrow, 0, NCOL - 1),
                                "innerHorizontal": {"style": "SOLID",
                                                    "color": BORDER}}})
    q.append({"updateBorders": {"range": gr(sid, 3, 4, 0, NCOL - 1),
                                "bottom": {"style": "SOLID_MEDIUM", "color": NAVY}}})
    q.append(repeat(sid, nrow, ntotal, 0, NCOL, {
        "textFormat": {"foregroundColor": NAVY, "italic": True, "fontSize": 10}}))
    q.append(note(sid, 3, 6, "condition = 100 minus four deductions: autonomic "
                             "(HRV against the personal band, lightened by how "
                             "well recent training explains the drop), sleep "
                             "(last night plus accumulated debt, never excused "
                             "by training), somatic (RHR, respiration, daytime "
                             "rest) and secondary (stress, overnight recharge). "
                             "The colours follow the labels: 90+ strong / 66-89 "
                             "fine / 50-65 watch / below 50 unwell. The bracket "
                             "names the largest single deduction of the day, "
                             "and a trailing * means an input was missing."))
    q.append(note(sid, 3, 9, "Garmin activityTrainingLoad (derived from EPOC, "
                             "unitless). For a weekly total, read the target "
                             "band on the Status tab (CTL x 7)."))
    q.append(note(sid, 3, 4, "A quality day shows the menu on the first line "
                             "and one result line per set (splits, HRmax, "
                             "running watts). An easy day stays on one line."))
    q.append(note(sid, 3, 7, "The arrow on HRV means the value moved beyond one "
                             "standard deviation of its own preceding 28 days. "
                             "It is a statistical mark, not a health verdict. "
                             "The condition score judges HRV against Garmin's "
                             "personal band, which is a different threshold, so "
                             "a day can lose condition points with no arrow."))
    q.append(note(sid, 3, 2, classifier_note(gates, acwr_lo, acwr_hi)))
    # Conditional formats: exception markers only, no cell shading.
    q.append(boolrule(sid, [gr(sid, 4, nrow, 2, 3)], {
        "type": "CUSTOM_FORMULA",
        "values": [{"userEnteredValue": f'=REGEXMATCH($C5,{QUALITY_RE})'}]},
        {"textFormat": {"foregroundColor": NAVY, "bold": True}}))
    q.append(boolrule(sid, [gr(sid, 4, nrow, 8, 9)], {
        "type": "CUSTOM_FORMULA",
        "values": [{"userEnteredValue":
                    f'=AND($I5<>"",OR($I5<{acwr_lo},$I5>{acwr_hi}))'}]},
        {"textFormat": {"foregroundColor": NAVY, "bold": True}}))
    # Condition: unmarked at or above the top boundary, escalating below it.
    # The conditions are mutually exclusive so rule order cannot bite.
    g = [gr(sid, 4, nrow, 6, 7)]
    for cond, f in [
        ('=AND($G5<>"",$G5<50)', {"foregroundColor": RED, "bold": True}),
        ('=AND($G5<>"",$G5>=50,$G5<66)', {"foregroundColor": AMBER, "bold": True}),
        ('=AND($G5<>"",$G5>=66,$G5<90)', {"bold": True}),
    ]:
        q.append(boolrule(sid, g, {"type": "CUSTOM_FORMULA",
                                   "values": [{"userEnteredValue": cond}]},
                          {"textFormat": f}))
    q.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0,
                  "endIndex": 1},
        "properties": {"pixelSize": 34}, "fields": "pixelSize"}})
    q.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 2,
                  "endIndex": 3},
        "properties": {"pixelSize": 26}, "fields": "pixelSize"}})
    return q


# --------------------------------------------------------------------------- #
def build_parser():
    today = dt.date.today()
    ap = argparse.ArgumentParser(
        prog="python -m training_log.month_view",
        description="Build the human-facing monthly view tab from the Log tab.")
    ap.add_argument("--year", type=int, default=today.year)
    ap.add_argument("--month", type=int, default=today.month)
    ap.add_argument("--tab", type=str, default=None,
                    help="tab name (default: the month's abbreviation, e.g. Jul)")
    ap.add_argument("--write", action="store_true",
                    help="write the tab; without it this only previews")
    ap.add_argument("--allow-new-tab", action="store_true",
                    help="permit creating a tab that does not exist yet")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    year, month = args.year, args.month
    if not 1 <= month <= 12:
        build_parser().error("--month must be 1..12")
    tab = args.tab or calendar.month_abbr[month]
    dry = not args.write

    athlete = config.athlete()
    gates = classify.Gates.from_athlete(athlete)
    acwr_lo, acwr_hi = athlete.section("status")["acwr_safe"]

    grid, day_rows, sub_rows = build_rows(year, month, gates)
    nrow = len(grid)

    # The preview reads the sheet too. Seeding and salvage decide what actually
    # lands in the hand columns, so a grid printed before them is a grid that
    # will never be written.
    sh = config.spreadsheet()
    ws = None
    try:
        ws = sh.worksheet(tab)
    except Exception:
        names = [w.title for w in sh.worksheets()]
        if not args.allow_new_tab:
            act = "previewing a tab that would be created" if dry else "creating it"
            print(f"\nREFUSED: tab '{tab}' does not exist in this spreadsheet, "
                  f"and {act} needs --allow-new-tab.")
            print(f"  existing tabs: {', '.join(names)}")
            print("  If you meant an existing month, pass its exact name with --tab.")
            print("  If this really is a new month, add --allow-new-tab.")
            return 2
        if dry:
            print(f"(tab '{tab}' would be CREATED - salvage skipped in preview)")
            print(f"  existing tabs: {', '.join(names)}")
        else:
            ws = sh.add_worksheet(title=tab, rows=len(grid) + 5, cols=NCOL)
            print(f"(created tab '{tab}')")

    # Seed the note column from Log once, so archived months carry their notes.
    # The plan column is NOT seeded: the two columns hold different things --
    # Log's race column is what happened, this tab's plan column is what was
    # intended -- and pouring one into the other resurrects entries that were
    # deleted on purpose.
    lvals = sh.worksheet(log_io.TAB).get_all_values()
    memo_i = log_io.HEADER.index("note")
    byd = {r[0]: r for r in lvals[1:] if r and r[0]}
    seeded = 0
    for day, r in enumerate(day_rows, 1):
        lr = byd.get(f"{year}-{month:02d}-{day:02d}")
        if not lr:
            continue
        if len(lr) > memo_i and lr[memo_i].strip() and not grid[r - 1][HAND["note"]]:
            grid[r - 1][HAND["note"]] = lr[memo_i]
            seeded += 1
    if seeded:
        print(f"(seeded {seeded} hand cells from {log_io.TAB})")

    # Salvage the hand columns if the tab already has day rows.
    old = ws.get_all_values() if ws is not None else []
    hand = {}
    for r in old:
        if r and r[0] and "/" in r[0].split(" ")[0]:
            for name, ci in HAND.items():
                if len(r) > ci and str(r[ci]).strip():
                    hand[(r[0], ci)] = r[ci]
    for i, row in enumerate(grid):
        for name, ci in HAND.items():
            v = hand.get((row[0], ci))
            if v:
                grid[i][ci] = v
    if hand:
        print(f"(salvaged {len(hand)} hand cells)")

    for row in grid:
        print(" | ".join(str(c)[:28] for c in row[:11]))
    if dry:
        print(f"\n[preview] {nrow} rows planned for tab '{tab}'. "
              f"Add --write to apply.")
        return 0

    sid = ws.id
    ws.resize(rows=max(nrow + 5, 45), cols=NCOL)
    # Drop this sheet's existing conditional formats so a re-run is idempotent.
    meta = sh.fetch_sheet_metadata(
        {"fields": "sheets(properties.sheetId,conditionalFormats)"})
    nold = 0
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] == sid:
            nold = len(s.get("conditionalFormats", []))
    dels = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}}
            for _ in range(nold)]

    ws.update(values=grid, range_name=f"A1:T{nrow}",
              value_input_option="USER_ENTERED")
    sh.batch_update({"requests": dels + fmt_requests(
        sid, sub_rows[-1], sub_rows, nrow, gates, acwr_lo, acwr_hi)})
    print(f"\nWrote '{tab}': {len(day_rows)} day rows, "
          f"{len(sub_rows)} week subtotals, formatting applied (sheetId {sid}).")
    return 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
