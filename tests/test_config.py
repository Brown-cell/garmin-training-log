# -*- coding: utf-8 -*-
"""Configuration refuses to guess, and says what is missing."""
import os

import pytest

from training_log import config


def test_missing_sheet_id_names_the_variable(monkeypatch):
    monkeypatch.delenv("TRAINING_LOG_SHEET_ID", raising=False)
    with pytest.raises(config.ConfigError) as e:
        config.sheet_id()
    assert "TRAINING_LOG_SHEET_ID" in str(e.value)


def test_missing_service_account_names_the_variable(monkeypatch):
    monkeypatch.delenv("TRAINING_LOG_SA_JSON", raising=False)
    with pytest.raises(config.ConfigError) as e:
        config.service_account_path()
    assert "TRAINING_LOG_SA_JSON" in str(e.value)


def test_service_account_path_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAINING_LOG_SA_JSON", str(tmp_path / "nope.json"))
    with pytest.raises(config.ConfigError) as e:
        config.service_account_path()
    assert "does not exist" in str(e.value)


def test_missing_athlete_file_points_at_the_example(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        config.load_athlete(str(tmp_path / "athlete.json"))
    assert "athlete.example.json" in str(e.value)


def test_bad_athlete_json_is_reported_as_such(tmp_path):
    p = tmp_path / "athlete.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.ConfigError) as e:
        config.load_athlete(str(p))
    assert "valid JSON" in str(e.value)


def test_missing_section_names_the_section(tmp_path):
    p = tmp_path / "athlete.json"
    p.write_text('{"races": []}', encoding="utf-8")
    a = config.load_athlete(str(p))
    with pytest.raises(config.ConfigError) as e:
        a.section("classifier")
    assert "classifier" in str(e.value)


def test_garmin_tokens_dir_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("GARMIN_TOKENS_DIR", raising=False)
    assert config.garmin_tokens_dir().endswith(".garminconnect")
    monkeypatch.setenv("GARMIN_TOKENS_DIR", os.path.join("some", "where"))
    assert config.garmin_tokens_dir() == os.path.join("some", "where")


def test_underscore_keys_are_documentation_not_settings(athlete):
    """The _README keys in the example file must not reach the model."""
    for name in ("baselines", "classifier", "condition_score", "pmc", "status"):
        assert not [k for k in athlete.section(name) if k.startswith("_")]


def test_baselines_have_the_shape_the_scorers_expect(athlete):
    b = athlete.baselines
    assert set(b) == {"rhr", "resp"}
    assert b["rhr"] > 0 and b["resp"] > 0


def test_races_are_sorted_pairs(athlete):
    races = athlete.races
    assert races == sorted(races)
    assert all(len(r) == 2 for r in races)
