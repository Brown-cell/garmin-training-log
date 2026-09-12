# -*- coding: utf-8 -*-
"""Test environment: dummy credentials and the example athlete file.

Nothing in the suite reaches the network. The credentials below are deliberate
nonsense -- they only have to exist, because the code refuses to run without
them, and no test gets as far as authenticating with anything.
"""
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXAMPLE_ATHLETE = ROOT / "athlete.example.json"


@pytest.fixture(autouse=True, scope="session")
def _environment():
    os.environ["TRAINING_LOG_SHEET_ID"] = "test-spreadsheet-key"
    os.environ["TRAINING_LOG_SA_JSON"] = str(EXAMPLE_ATHLETE)   # any real path
    os.environ["TRAINING_LOG_ATHLETE_JSON"] = str(EXAMPLE_ATHLETE)
    os.environ["TRAINING_LOG_DIR"] = str(ROOT / "logs")
    yield


@pytest.fixture()
def athlete(_environment):
    from training_log import config
    return config.load_athlete(str(EXAMPLE_ATHLETE))


@pytest.fixture()
def gates(athlete):
    from training_log import classify
    return classify.Gates.from_athlete(athlete)
