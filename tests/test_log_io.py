# -*- coding: utf-8 -*-
"""The column policy, against a fake worksheet.

The gspread client is replaced by a recorder, so these run with no credentials
and no network. What is checked is the rule the whole project rests on: who is
allowed to write which column.
"""
import datetime as dt

import pytest

from training_log import log_io, refresh

HEADER = log_io.HEADER
COL = {name: i + 1 for i, name in enumerate(HEADER)}


class FakeWorksheet:
    """Records batch_update calls instead of performing them."""

    def __init__(self, rows):
        self.rows = rows
        self.batches = []
        self.appended = []
        self.sorted_by = None

    def get_all_values(self):
        return self.rows

    def batch_update(self, batch, value_input_option=None):
        self.batches.append(batch)

    def append_rows(self, rows, **kw):
        self.appended.append(rows)

    def sort(self, *spec):
        self.sorted_by = spec


def row_for(date, **cells):
    r = [""] * len(HEADER)
    r[0] = date
    r[1] = "Wed"
    for name, v in cells.items():
        r[HEADER.index(name)] = v
    return r


def grid_with(*rows):
    return [list(HEADER)] + list(rows)


# --------------------------------------------------------------------------- #
# primitives                                                                   #
# --------------------------------------------------------------------------- #
def test_column_letters():
    assert log_io.colA1(0) == "A"
    assert log_io.colA1(25) == "Z"
    assert log_io.colA1(26) == "AA"


def test_parse_km_sums_every_distance_it_finds():
    assert log_io.parse_km("8.20km 42min + jog 1.20km") == 9.4
    assert log_io.parse_km("5×1000m") == ""
    assert log_io.parse_km("") == ""


def test_write_cells_addresses_the_right_squares():
    ws = FakeWorksheet(grid_with(row_for("2026-07-01")))
    n = log_io.write_cells(ws, [(5, 4, "Jog"), (5, 26, "note")])
    assert n == 2
    ranges = [b["range"] for b in ws.batches[0]]
    assert ranges == ["D5", "Z5"]


def test_write_cells_does_nothing_when_there_is_nothing_to_write():
    ws = FakeWorksheet(grid_with())
    assert log_io.write_cells(ws, []) == 0
    assert ws.batches == []


def test_the_three_column_sets_do_not_overlap():
    assert not (log_io.HAND_COLS & log_io.FILL_EMPTY_COLS)
    assert not (log_io.HAND_COLS & log_io.GARMIN_COLS)
    assert not (log_io.FILL_EMPTY_COLS & log_io.GARMIN_COLS)


# --------------------------------------------------------------------------- #
# the policy                                                                   #
# --------------------------------------------------------------------------- #
def test_hand_columns_are_never_written():
    cells = {"note": "felt awful", "event": "5000 m", "kind": "Jog", "load": 90}
    got = refresh.writable(cells, 7, COL)
    written = {c for (_, c, _) in got}
    assert COL["note"] not in written
    assert COL["event"] not in written
    assert written == {COL["kind"], COL["load"]}


def test_columns_absent_from_the_sheet_are_skipped():
    got = refresh.writable({"kind": "Jog", "not_a_column": 1}, 7, COL)
    assert got == [(7, COL["kind"], "Jog")]


@pytest.fixture()
def policy_env(monkeypatch, athlete, gates):
    """day_cells with the two Garmin round-trips replaced by fixtures."""
    garmin = {"load": 90, "sleep_h": 7.5, "km": 8.0, "cond_score": 91}
    derived = {"kind": "Jog", "km": 8.0, "menu": "8.00km 40min", "result": "5:00/km"}
    monkeypatch.setattr(log_io, "garmin_cells",
                        lambda g, iso, athlete=None: dict(garmin))
    monkeypatch.setattr(log_io, "running_cells",
                        lambda g, iso, gates=None, race=False: dict(derived))
    return athlete, gates, garmin, derived


def call(grid, policy_env, sync=None, today=None):
    athlete, gates, _, _ = policy_env
    today = today or dt.date(2026, 7, 3)
    sync = sync or dt.datetime(2026, 7, 3, 6, 0)
    return refresh.day_cells(None, "2026-07-01", 2, grid, COL, athlete, gates,
                             sync, today)


def test_an_empty_row_is_filled_from_garmin(policy_env):
    grid = grid_with(row_for("2026-07-01"))
    cells, _ = call(grid, policy_env)
    assert cells["kind"] == "Jog"
    assert cells["menu"] == "8.00km 40min"
    assert cells["load"] == 90


def test_a_hand_written_label_is_left_alone(policy_env):
    grid = grid_with(row_for("2026-07-01", kind="VO2", menu="5x1000m",
                             result="3:05"))
    cells, _ = call(grid, policy_env)
    assert "kind" not in cells                   # not offered for writing at all
    assert "menu" not in cells
    assert cells["load"] == 90                   # Garmin columns still refresh


def test_only_the_empty_session_columns_are_filled(policy_env):
    grid = grid_with(row_for("2026-07-01", kind="VO2"))
    cells, _ = call(grid, policy_env)
    assert "kind" not in cells
    assert cells["menu"] == "8.00km 40min"        # this one was empty


def test_a_rest_placeholder_is_re_derived_when_running_appears(policy_env):
    grid = grid_with(row_for("2026-07-01", kind="rest"))
    cells, note = call(grid, policy_env)
    assert cells["kind"] == "Jog"
    assert "healed" in note


def test_a_stale_automatic_jog_is_re_derived(policy_env, monkeypatch):
    """Machine-written text plus more running than it accounts for."""
    monkeypatch.setattr(log_io, "garmin_cells",
                        lambda g, iso, athlete=None: {"km": 14.0, "load": 120})
    monkeypatch.setattr(log_io, "running_cells",
                        lambda g, iso, gates=None, race=False:
                        {"kind": "Jog", "km": 14.0, "menu": "8.00km 40min + 6.00km 30min",
                         "result": "5:00/km"})
    grid = grid_with(row_for("2026-07-01", kind="Jog", menu="8.00km 40min",
                             result="5:00/km"))
    cells, note = call(grid, policy_env)
    assert "6.00km" in cells["menu"]
    assert "healed" in note


def test_hand_written_text_is_not_treated_as_stale(policy_env, monkeypatch):
    monkeypatch.setattr(log_io, "garmin_cells",
                        lambda g, iso, athlete=None: {"km": 14.0})
    grid = grid_with(row_for("2026-07-01", kind="Jog",
                             menu="easy loop round the park", result="felt fine"))
    cells, note = call(grid, policy_env)
    assert "menu" not in cells
    assert note == ""


def test_rest_needs_proof_that_the_watch_synced(policy_env, monkeypatch):
    monkeypatch.setattr(log_io, "running_cells",
                        lambda g, iso, gates=None, race=False:
                        {"kind": "rest", "km": "", "menu": "", "result": ""})
    grid = grid_with(row_for("2026-07-01"))

    cells, note = call(grid, policy_env, sync=dt.datetime(2026, 7, 3, 6, 0))
    assert cells["kind"] == "rest"

    cells, note = call(grid, policy_env, sync=dt.datetime(2026, 6, 30, 6, 0))
    assert "kind" not in cells                   # no evidence -> leave it blank
    assert "no sync evidence" in note


def test_today_is_never_marked_rest(policy_env, monkeypatch):
    monkeypatch.setattr(log_io, "running_cells",
                        lambda g, iso, gates=None, race=False:
                        {"kind": "rest", "km": "", "menu": "", "result": ""})
    grid = grid_with(row_for("2026-07-01"))
    cells, note = call(grid, policy_env, today=dt.date(2026, 7, 1),
                       sync=dt.datetime(2026, 7, 1, 20, 0))
    assert "kind" not in cells


def test_the_race_column_reaches_the_classifier(policy_env, monkeypatch):
    seen = {}

    def running_cells(g, iso, gates=None, race=False):
        seen["race"] = race
        return {"kind": "race", "km": 3.0, "menu": "3000m", "result": "9:05"}

    monkeypatch.setattr(log_io, "running_cells", running_cells)
    grid = grid_with(row_for("2026-07-01", event="club meet 3000m"))
    cells, _ = call(grid, policy_env)
    assert seen["race"] is True
    assert cells["kind"] == "race"


def test_an_ordinary_note_in_the_race_column_is_not_a_race(policy_env, monkeypatch):
    seen = {}

    def running_cells(g, iso, gates=None, race=False):
        seen["race"] = race
        return {"kind": "Jog", "km": 8.0, "menu": "8.00km 40min", "result": ""}

    monkeypatch.setattr(log_io, "running_cells", running_cells)
    grid = grid_with(row_for("2026-07-01", event="dentist"))
    call(grid, policy_env)
    assert seen["race"] is False
