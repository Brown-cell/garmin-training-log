# -*- coding: utf-8 -*-
"""Nightly forward update of the Log tab: the living-log refresh.

For a window of recent dates (default: the last 10 days through today):

  * make sure a row exists for each date, keyed by ISO date,
  * REFRESH every Garmin-derived cell (wellness, scores, activity, load),
  * FILL the session columns only when they are empty, so a hand-written label
    always wins,
  * NEVER touch the hand columns.

Self-healing, because the watch does not always sync before the job runs:

  * "rest" is only written for a COMPLETED day the watch has uploaded past.
    With no sync evidence the cell is left blank, and a later run in the window
    fills it once the data arrives. Writing rest and moving on instead turns a
    sync delay into a permanent lie in the training history.
  * A placeholder the pipeline itself wrote is not a hand label. If Garmin
    later shows real running on such a day, the session columns are re-derived
    and overwritten. Real hand labels are never touched.
  * A stale automatic jog is re-derived too: the label is "Jog", the menu is
    still in the machine's own format, and Garmin now has more running than
    that menu accounts for (an evening second run that synced after the job).

Idempotent and non-destructive: re-run it whenever. Rows are found by date, so
it is safe to run while a historical backfill is running.

  python -m training_log.refresh                 # last 10 days
  python -m training_log.refresh --days 30
  python -m training_log.refresh --since 2026-07-01
  python -m training_log.refresh --dry-run
"""
import argparse
import datetime as dt
import re
import sys

from . import classify, config, garmin_fetch, log_io

# Labels this pipeline writes when it does not know better. They are safe to
# re-derive; anything else in that column came from a human.
PLACEHOLDERS = {"rest"}

# A race column entry matching this marks the day as a race, so it is filed as
# a race rather than as whichever training zone its heart rate resembled.
RACE_RE = re.compile(r"race|TT|time trial|meet|competition|championship", re.I)

# A menu string in the machine's own jog format ("8.20km 42min + jog 1.2km").
# Anything else in that column is hand-written text and is left alone.
AUTO_DETAIL_RE = re.compile(
    r"^[\d.]+km( \d+min)?(\s*\+\s*(jog )?[\d.]+km( \d+min)?)*$")

STALE_JOG_KM = 0.8      # km of unexplained running that makes a jog stale


def cur(grid, row1, col1):
    """The current text of a cell, from the grid already read."""
    r = grid[row1 - 1] if row1 - 1 < len(grid) else []
    return r[col1 - 1].strip() if (col1 and col1 - 1 < len(r)) else ""


def sync_covers(sync, iso, today):
    """Has the watch uploaded past the end of this (past) day?"""
    d = dt.date.fromisoformat(iso)
    if d >= today:
        return False                          # never write rest for an open day
    return sync is not None and sync > dt.datetime.combine(
        d + dt.timedelta(days=1), dt.time.min)


def day_cells(g, iso, row, grid, col_of, athlete, gates, sync, today):
    """The whole column policy for one day -> ({column: value}, note).

    This is the only place that decides what may be written, so the forward
    refresh and the historical backfill cannot drift apart. Hand columns are
    filtered by the caller as well; they are simply never produced here.
    """
    cells = log_io.garmin_cells(g, iso, athlete=athlete)      # always refreshed
    note = ""

    old_kind = cur(grid, row, col_of.get("kind"))
    old_detail = cur(grid, row, col_of.get("menu"))
    race_cell = (cur(grid, row, col_of.get("event"))
                 or cur(grid, row, col_of.get("plan")))
    race = bool(RACE_RE.search(race_cell))

    need_fill = any(not cur(grid, row, col_of.get(n))
                    for n in log_io.FILL_EMPTY_COLS)
    heal = old_kind.lower() in PLACEHOLDERS
    stale_jog = (old_kind.lower() == "jog"
                 and AUTO_DETAIL_RE.match(old_detail or "")
                 and isinstance(cells.get("km"), (int, float))
                 and abs(cells["km"] - (log_io.parse_km(old_detail) or 0))
                 > STALE_JOG_KM)

    if need_fill or heal or stale_jog:
        run = log_io.running_cells(g, iso, gates=gates, race=race)
        if run.get("kind") == "rest":
            if not old_kind and sync_covers(sync, iso, today):
                cells["kind"] = "rest"                # confirmed no-run day
            elif not old_kind:
                note = " (no sync evidence - left blank)"
        elif heal or stale_jog:                      # re-derive a pipeline value
            for name in ("kind", "menu", "result"):
                if run.get(name) not in (None, ""):
                    cells[name] = run[name]
            note = f" (healed: {old_kind or '<empty>'} -> {run['kind']})"
        else:                                        # ordinary fill-empty
            for name in log_io.FILL_EMPTY_COLS:
                if (not cur(grid, row, col_of.get(name))
                        and run.get(name) not in (None, "")):
                    cells[name] = run[name]
    return cells, note


def writable(cells, row, col_of):
    """[(row, col, value)] for the cells that exist and are not hand columns."""
    out = []
    for name, val in cells.items():
        ci = col_of.get(name)
        if not ci or name in log_io.HAND_COLS:
            continue
        out.append((row, ci, val))
    return out


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m training_log.refresh",
        description="Refresh recent days of the Log tab from Garmin Connect.")
    ap.add_argument("--days", type=int, default=10,
                    help="how many days back to refresh (default: 10)")
    ap.add_argument("--since", type=str, default=None,
                    help="refresh from this ISO date through today instead")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written, write nothing")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    today = dt.date.today()
    start = (dt.date.fromisoformat(args.since) if args.since
             else today - dt.timedelta(days=args.days - 1))
    dates = [(start + dt.timedelta(days=i)).isoformat()
             for i in range((today - start).days + 1)]

    athlete = config.athlete()
    gates = classify.Gates.from_athlete(athlete)
    sh = config.spreadsheet()
    ws, col_of, row_of, grid = log_io.open_log(sh)
    if not args.dry_run:
        col_of, row_of, grid = log_io.ensure_rows(ws, dates, sh)

    g = config.garmin_client()
    sync = garmin_fetch.last_sync_local(g)

    updates, touched = [], 0
    for iso in dates:
        row = row_of.get(iso)
        if row is None:                       # dry run: the row does not exist yet
            print(f"{iso}: (row would be created)")
            continue
        old_kind = cur(grid, row, col_of.get("kind"))
        cells, note = day_cells(g, iso, row, grid, col_of, athlete, gates,
                                sync, today)
        wrote = writable(cells, row, col_of)
        updates += wrote
        if wrote:
            touched += 1
        print(f"{iso} r{row}: sleep {cells.get('sleep_h', '-')} "
              f"condition {cells.get('cond_score', '-')}"
              f"{cells.get('cond_label', '-')} "
              f"kind {cells.get('kind', old_kind or '-')} "
              f"ACWR {cells.get('ACWR', '-')}  [{len(wrote)} cells]{note}")

    if args.dry_run:
        print(f"\n[dry run] {len(updates)} cells across {touched} days "
              f"would be written.")
        return 0
    n = log_io.write_cells(ws, updates)
    print(f"\nWrote {n} cells across {touched} days into '{log_io.TAB}'.")
    return 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
