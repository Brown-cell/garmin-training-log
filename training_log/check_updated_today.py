# -*- coding: utf-8 -*-
"""Has today's update already run? Answered from the sheet, not from memory.

Read-only. Useful before running the pipeline by hand, and in any wrapper that
must not run it twice.

What it reads (the file modification time in Drive is not available: a
service-account key scoped to spreadsheets only gets a 403 for it):

  Status A1        the date stamp the dashboard writes when it rebuilds. If
                   that is today, the dashboard ran today.
  Log's last data  how far the refresh filled the Garmin columns. A Status
                   stamped today over a Log that stopped days ago means the
                   refresh step is failing while the dashboard step succeeds.

The resolution is one day, which is all "did it run today" needs.

  python -m training_log.check_updated_today

Exit codes: 0 already ran today / 1 not yet / 2 partial (Log is stale)
"""
import datetime as dt
import re
import sys

from . import config, log_io

GARMIN_EVIDENCE = ("steps", "sleep_h", "load", "readiness")
STALE_DAYS = 2


def status_stamp(sh):
    """The date in Status!A1, or None."""
    try:
        a1 = sh.worksheet(config.STATUS_TAB).acell("A1").value or ""
    except Exception as e:
        print(f"  (could not read the {config.STATUS_TAB} tab: {type(e).__name__})")
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", a1)
    return dt.date.fromisoformat(m.group(1)) if m else None


def log_last_data_day(sh):
    """The latest date with any Garmin-derived column filled in."""
    _ws, col_of, _row_of, grid = log_io.open_log(sh)
    date_i = col_of["date"] - 1
    ev = [col_of[k] - 1 for k in GARMIN_EVIDENCE if k in col_of]
    best = None
    for r in grid[1:]:
        try:
            d = dt.date.fromisoformat(str(r[date_i]).strip())
        except (TypeError, ValueError, IndexError):
            continue
        for i in ev:
            if i < len(r) and str(r[i]).strip():
                if best is None or d > best:
                    best = d
                break
    return best


def ago(d, today):
    n = (today - d).days
    return "today" if n == 0 else ("yesterday" if n == 1 else f"{n} days ago")


def main(argv=None):
    today = dt.date.today()
    sh = config.spreadsheet()
    st = status_stamp(sh)
    lg = log_last_data_day(sh)

    print(f"today            : {today}")
    print(f"Status stamped   : {st} ({ago(st, today)})" if st else
          "Status stamped   : could not read")
    print(f"Log last data day: {lg} ({ago(lg, today)})" if lg else
          "Log last data day: could not read")
    print()

    ran = (st == today)
    stale = (lg is None) or ((today - lg).days >= STALE_DAYS)
    if ran and not stale:
        print("Already ran today. Nothing to do.")
        return 0
    if ran and stale:
        print("Partial: the dashboard was rebuilt today but the Log data is "
              "stale. The refresh step is the one to look at.")
        return 2
    print("Has not run today.")
    return 1


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
