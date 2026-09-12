# -*- coding: utf-8 -*-
"""The Log tab: its schema, its column policy, and date-keyed upsert.

`Log` is the database. One row per calendar day, ISO date in column A as the
primary key, no merged cells, no formulas, scores split from their labels. The
month view and the Status dashboard are what a human reads. Everything that
writes to `Log` goes through here, so the rules below exist in exactly one
place.

**The column policy is the safety rule of the whole project.** Each column
belongs to exactly one writer:

  HAND_COLS        the athlete's. Never written by the pipeline at all.
  FILL_EMPTY_COLS  shared. Written only into an empty cell, so a hand-written
                   label always wins and re-running is safe.
  GARMIN_COLS      the watch's. Always overwritten, because Garmin is
                   authoritative for them and a stale value is a wrong value.

A column may not be in two sets, and the pipeline may not decide at runtime
which set a column is in. Anything that wants to overwrite a shared column has
to justify it at the call site (see `refresh.py`, where the only two exceptions
are re-deriving a placeholder and refreshing a stale automatic jog).
"""
import datetime as dt
import re
import time

from . import classify, config, garmin_fetch, wellness

TAB = config.LOG_TAB

# Weekday names as they appear in the sheet (Mon..Sun).
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# The schema. Add new columns AT THE END only: readers that resolve a column by
# position would otherwise start reading its neighbour.
#
#   date wd            ISO date, weekday
#   event kind km      race or event (hand), session label, distance
#   menu result        what was done, how it went
#   sleep_h..readiness raw Garmin wellness
#   sleep_score..cond_label  derived scores and their labels
#   steps..load        movement and load
#   note               free note (hand)
HEADER = ["date", "wd", "event", "kind", "km", "menu", "result",
          "sleep_h", "deep_h", "rem_h", "awake_h", "HRV", "RHR", "readiness",
          "sleep_score", "sleep_label", "cond_score", "cond_label",
          "steps", "floors", "intensity_min", "bb_drained", "ACWR",
          "recovery_h", "load", "note"]

# `plan` is accepted as well as `event`: the column has been called both, and a
# hand column that loses its protection because it was renamed is the one
# mistake this set exists to prevent.
HAND_COLS = {"event", "plan", "note"}
FILL_EMPTY_COLS = {"kind", "menu", "result"}
GARMIN_COLS = {"sleep_h", "deep_h", "rem_h", "awake_h", "HRV", "RHR",
               "readiness", "sleep_score", "sleep_label", "cond_score",
               "cond_label", "steps", "floors", "intensity_min", "bb_drained",
               "ACWR", "recovery_h", "load", "km"}


def colA1(idx):
    """0-based column index -> A1 letters."""
    s, i = "", idx + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_km(detail):
    """Total of the distances written in a menu string, or ''."""
    if not detail:
        return ""
    kms = re.findall(r"([\d.]+)\s*km", str(detail))
    return round(sum(float(k) for k in kms), 2) if kms else ""


# --------------------------------------------------------------------------- #
# sheet I/O                                                                    #
# --------------------------------------------------------------------------- #
def open_log(sh=None):
    """(worksheet, {column name: 1-based col}, {ISO date: 1-based row}, grid).

    Columns are resolved from the sheet's OWN header row, never from HEADER, so
    a sheet whose columns were reordered still works.
    """
    sh = sh or config.spreadsheet()
    ws = sh.worksheet(TAB)
    grid = ws.get_all_values()
    hdr = grid[0] if grid else []
    col_of = {name: i + 1 for i, name in enumerate(hdr) if str(name).strip()}
    if "date" not in col_of:
        raise config.ConfigError(
            f"The '{TAB}' tab has no 'date' column in row 1. Create the tab with "
            f"`python -m training_log.backfill --create-tab` first.")
    di = col_of["date"] - 1
    row_of = {}
    for r in range(1, len(grid)):
        v = grid[r][di].strip() if di < len(grid[r]) else ""
        if v:
            row_of[v] = r + 1
    return ws, col_of, row_of, grid


def ensure_rows(ws, dates, sh=None):
    """Append a (date, weekday) row for every ISO date not present; re-read."""
    _, _, row_of, _ = open_log(sh)
    missing = sorted(d for d in dates if d not in row_of)
    if missing:
        rows = [[d, WD[dt.date.fromisoformat(d).weekday()]] for d in missing]
        ws.append_rows(rows, value_input_option="USER_ENTERED", table_range="A1")
    _, col_of, row_of, grid = open_log(sh)
    return col_of, row_of, grid


def write_cells(ws, updates, retries=3):
    """updates: [(row_1based, col_1based, value)]. One batched A1 update.

    The batch is rebuilt on every attempt: gspread qualifies each range with
    the worksheet title in place, so a retried list comes back as
    "'Log'!'Log'!AE1" and the parse error that raises hides whatever actually
    failed the first time.
    """
    if not updates:
        return 0
    for attempt in range(retries):
        batch = [{"range": f"{colA1(c - 1)}{r}", "values": [[v]]}
                 for (r, c, v) in updates]
        try:
            ws.batch_update(batch, value_input_option="USER_ENTERED")
            return len(batch)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def sort_by_date(ws):
    ws.sort((1, "asc"))


def create_log_tab(sh=None, rows=400):
    """Create an empty Log tab with the header row. Refuses to touch an
    existing one -- recreating this tab would drop every hand-written cell."""
    sh = sh or config.spreadsheet()
    try:
        sh.worksheet(TAB)
    except Exception:
        pass
    else:
        raise config.ConfigError(
            f"A '{TAB}' tab already exists; refusing to recreate it. Delete it "
            f"by hand first if that is really what you want.")
    ws = sh.add_worksheet(title=TAB, rows=rows, cols=len(HEADER) + 2)
    ws.update(values=[HEADER], range_name=f"A1:{colA1(len(HEADER) - 1)}1",
              value_input_option="USER_ENTERED")
    ws.format(f"A1:{colA1(len(HEADER) - 1)}1", {"textFormat": {"bold": True}})
    ws.freeze(rows=1)
    return ws


# --------------------------------------------------------------------------- #
# value builders                                                               #
# --------------------------------------------------------------------------- #
def garmin_cells(g, iso, athlete=None):
    """{column: value} of everything Garmin is authoritative for, one day.

    Empty values are dropped so a failed fetch blanks nothing, with two
    exceptions where zero is a real measurement rather than a missing one:
    intensity minutes and recovery hours are both legitimately 0 on a rest day.
    """
    a = athlete or config.athlete()
    o = garmin_fetch.fetch_day(g, iso, athlete=a)
    act = garmin_fetch.fetch_daily_activity(g, iso)
    ss, sl, _ = wellness.sleep_score(o)
    cs, cl, _ = wellness.condition_score(o, a.baselines, wellness.params(a))
    c = {}

    def put(k, v):
        if v is not None and v != "":
            c[k] = v

    if o.get("sleep_s"):
        put("sleep_h", round(o["sleep_s"] / 3600, 1))
        put("deep_h", round((o.get("deep_s") or 0) / 3600, 1))
        put("rem_h", round((o.get("rem_s") or 0) / 3600, 1))
        put("awake_h", round((o.get("awake_s") or 0) / 3600, 1))
    put("km", garmin_fetch.run_km(g, iso))
    put("HRV", o.get("hrv"))
    put("RHR", o.get("rhr"))
    put("readiness", o.get("readiness"))
    if ss is not None:
        put("sleep_score", ss)
        put("sleep_label", sl)
    if cs is not None:
        put("cond_score", cs)
        put("cond_label", cl)
    put("steps", act.get("steps"))
    put("floors", act.get("floors"))
    if act.get("intensity_min") is not None:
        c["intensity_min"] = act["intensity_min"]
    put("bb_drained", act.get("bb_drained"))
    put("ACWR", act.get("acwr"))
    if act.get("recovery_h") is not None:
        c["recovery_h"] = act["recovery_h"]
    put("load", act.get("session_load"))
    return c


def running_cells(g, iso, gates=None, race=False):
    """{kind, km, menu, result} derived from the day's activities."""
    acts = garmin_fetch._try(g.get_activities_by_date, iso, iso) or []
    kind, menu, data, _ = classify.build_entry(g, iso, acts, gates=gates, race=race)
    return {"kind": kind, "km": parse_km(menu), "menu": menu, "result": data}
