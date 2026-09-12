# -*- coding: utf-8 -*-
"""Snapshot every month tab's hand-written columns to a local file.

Why this exists: the plan column of the month tabs is the only address of
confirmed races and events. It is not in Log, and `month_view` only salvages it
from the tab it is about to rewrite -- so deleting, renaming or recreating a
tab loses it with no way back. A copy on disk is enough, provided whatever
backs up your machine picks it up; this deliberately does not build a second
backup system.

Read-only by construction: it calls `worksheets()` and `get_all_values()` and
nothing else. It must never write to the spreadsheet.

  python -m training_log.dump_hand_columns
  python -m training_log.dump_hand_columns --out somewhere/else.tsv

Exit codes:
  0  written
  2  a tab could not be read; the old snapshot is left untouched
  3  the sheet yielded no hand cells at all while the old snapshot had some.
     That is treated as a failed read rather than as "everything was deleted",
     because overwriting a good backup with an empty file is the one outcome
     this script exists to prevent.
"""
import argparse
import csv
import os
import re
import sys
import tempfile

from . import config

# Month tabs in either spelling, plus any suffix, so archive tabs come along.
# Anchored at the start so Log, Status and stray sheets are excluded.
MONTH_TAB = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
TITLE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")     # "2026年8月 練習日誌"
DAY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})\b")             # "8/11 月"
COLS = ("予定", "メモ")
FALLBACK = {"予定": 1, "メモ": 10}          # month_view.HAND


def find_hand_cols(rows):
    """Locate the hand columns by NAME in the tab's own header row.

    Reading the live header instead of trusting fixed indexes means a column
    inserted in month_view cannot quietly start snapshotting the wrong cells --
    the snapshot follows the rename instead.
    """
    for r in rows[:8]:
        if all(c in r for c in COLS):
            return {c: r.index(c) for c in COLS}, True
    return dict(FALLBACK), False


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m training_log.dump_hand_columns",
        description="Back up the month tabs' hand-written columns to a TSV.")
    ap.add_argument("--out", type=str, default=None,
                    help="output path (default: <TRAINING_LOG_DIR>/month_view_hand.tsv)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    out_path = args.out or str(config.log_dir() / "month_view_hand.tsv")

    sh = config.spreadsheet()
    out, scanned, degraded = [], [], []
    for ws in sh.worksheets():
        if not MONTH_TAB.match(ws.title):
            continue
        try:
            rows = ws.get_all_values()
        except Exception as e:
            # A partial snapshot is worse than a stale one: a tab that failed
            # to read looks exactly like a tab that was emptied.
            print(f"ERROR: could not read tab '{ws.title}': {e}")
            print("REFUSED: writing a partial snapshot would silently drop that "
                  f"tab's rows. '{out_path}' left untouched.")
            return 2
        hand, exact = find_hand_cols(rows)
        if not exact:
            degraded.append(ws.title)
        m = TITLE_RE.search(" ".join(rows[0]) if rows else "")
        year = int(m.group(1)) if m else None
        n = 0
        for r in rows:
            d = DAY_RE.match(r[0]) if r and r[0] else None
            if not d:
                continue
            vals = [r[i].strip() if len(r) > i else ""
                    for i in (hand["予定"], hand["メモ"])]
            if not any(vals):
                continue
            # ISO when the year is knowable, else the tab's own label. Never a
            # guessed year, and never a dropped row.
            date = (f"{year}-{int(d.group(1)):02d}-{int(d.group(2)):02d}"
                    if year else r[0])
            out.append([ws.title, date] + vals)
            n += 1
        scanned.append(f"{ws.title}:{n}")

    old = 0
    if os.path.exists(out_path):
        # csv.reader, not a line count: a note with an embedded newline is one
        # record spanning two physical lines, and this number is read during a
        # recovery, so it has to be true.
        with open(out_path, encoding="utf-8-sig", newline="") as f:
            old = max(0, sum(1 for _ in csv.reader(f, delimiter="\t")) - 1)
    if not out and old:
        print(f"REFUSED: read 0 hand cells but '{out_path}' holds {old} rows. "
              "Treating this as a failed read; the old snapshot is kept.")
        return 3

    # Write to a temp file in the same directory and replace: a crash or a full
    # disk mid-write must not leave a truncated backup.
    outdir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(outdir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=outdir, suffix=".tmp")
    os.close(fd)
    # Tab-delimited CSV rather than naive joining: note cells legitimately
    # contain newlines, which would otherwise corrupt every following row.
    # utf-8-sig so a spreadsheet application opens it correctly in a recovery.
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["tab", "date", "予定", "メモ"])
        w.writerows(out)
    os.replace(tmp, out_path)

    print(f"hand columns: {len(out)} rows -> {out_path}  [{' '.join(scanned)}]")
    if degraded:
        print(f"  WARNING: header row not found in {', '.join(degraded)}; "
              "fell back to fixed column indexes.")
    return 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
