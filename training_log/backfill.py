# -*- coding: utf-8 -*-
"""Fill the Log tab backwards over a date range -- the one-off first import.

Same column policy as the nightly refresh, and it calls the same function to
decide it (`refresh.day_cells`), so history and today are never filled by two
different sets of rules. What differs is only the shape of the job:

  * an explicit date range instead of a rolling window,
  * one write per day, so an interrupted run resumes where it stopped,
  * an optional pause between days, because several hundred days of history is
    a lot of requests to make at full speed against somebody else's API.

Resumable, idempotent, and safe to run while the nightly refresh runs: rows are
addressed by ISO date, never by position.

  python -m training_log.backfill --create-tab          # first time only
  python -m training_log.backfill --since 2026-01-01
  python -m training_log.backfill --since 2026-01-01 --until 2026-03-31
  python -m training_log.backfill --since 2026-01-01 --sleep 0.5
  python -m training_log.backfill --sort               # re-sort by date and exit
  python -m training_log.backfill --since 2026-01-01 --dry-run
"""
import argparse
import datetime as dt
import sys
import time

from . import classify, config, garmin_fetch, log_io, refresh


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m training_log.backfill",
        description="Import a range of past days into the Log tab.")
    ap.add_argument("--since", type=str, default=None,
                    help="first ISO date to import")
    ap.add_argument("--until", type=str, default=None,
                    help="last ISO date to import (default: today)")
    ap.add_argument("--create-tab", action="store_true",
                    help="create an empty Log tab with the header row, then exit")
    ap.add_argument("--sort", action="store_true",
                    help="re-sort the Log tab by date and exit")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to wait between days (default: 0)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written, write nothing")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.create_tab:
        ws = log_io.create_log_tab()
        print(f"Created the '{log_io.TAB}' tab with {len(log_io.HEADER)} columns.")
        print(f"  spreadsheet: {ws.spreadsheet.title}")
        return 0

    sh = config.spreadsheet()
    ws, col_of, row_of, grid = log_io.open_log(sh)

    if args.sort and not args.since:
        log_io.sort_by_date(ws)
        print(f"Sorted '{log_io.TAB}' by date.")
        return 0

    if not args.since:
        build_parser().error("--since is required (or use --sort / --create-tab)")
    today = dt.date.today()
    start = dt.date.fromisoformat(args.since)
    end = dt.date.fromisoformat(args.until) if args.until else today
    if end > today:
        end = today
    if start > end:
        build_parser().error("--since is after --until")
    dates = [(start + dt.timedelta(days=i)).isoformat()
             for i in range((end - start).days + 1)]
    print(f"=== {dates[0]} .. {dates[-1]}  ({len(dates)} days) ===")

    athlete = config.athlete()
    gates = classify.Gates.from_athlete(athlete)
    if not args.dry_run:
        col_of, row_of, grid = log_io.ensure_rows(ws, dates, sh)

    g = config.garmin_client()
    sync = garmin_fetch.last_sync_local(g)

    for iso in dates:
        row = row_of.get(iso)
        if row is None:                       # dry run: the row does not exist yet
            print(f"  {iso}: (row would be created)")
            continue
        cells, note = refresh.day_cells(g, iso, row, grid, col_of, athlete,
                                        gates, sync, today)
        ups = refresh.writable(cells, row, col_of)
        if args.dry_run:
            shown = ", ".join(f"{k}={v}" for k, v in list(cells.items())[:8])
            print(f"  {iso}: {shown}{' ...' if len(cells) > 8 else ''}{note}")
            continue
        log_io.write_cells(ws, ups)
        print(f"  {iso} r{row}: {len(ups)} cells "
              f"(kind={cells.get('種別', '-')} "
              f"condition={cells.get('体調評価', '-')} "
              f"ACWR={cells.get('ACWR', '-')}){note}")
        if args.sleep:
            time.sleep(args.sleep)

    if args.dry_run:
        print("\n[dry run] nothing written.")
        return 0
    log_io.sort_by_date(ws)
    print(f"\nDone. '{log_io.TAB}' re-sorted by date.")
    return 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
