# -*- coding: utf-8 -*-
"""Every piece of environment this package needs, resolved in one place.

Nothing else in `training_log` reads `os.environ`, and nothing has a default
that points at somebody's machine. Two kinds of setting live here:

  credentials / locations   environment variables, no defaults where a wrong
                            guess would write to the wrong spreadsheet
  athlete constants         `athlete.json`, a copy of `athlete.example.json`
                            with your own numbers in it

Environment variables:

  TRAINING_LOG_SHEET_ID      required. The spreadsheet key out of its URL:
                             docs.google.com/spreadsheets/d/<KEY>/edit
  TRAINING_LOG_SA_JSON       required. Path to the Google service-account key
                             file. The service account needs edit access to the
                             spreadsheet (share it with the account's address).
  TRAINING_LOG_ATHLETE_JSON  optional. Defaults to ./athlete.json
  TRAINING_LOG_DIR           optional. Where run logs and local snapshots go.
                             Defaults to ./logs
  GARMIN_TOKENS_DIR          optional. garminconnect's OAuth token cache.
                             Defaults to ~/.garminconnect

The Garmin side has no username/password setting: log in once interactively
with garminconnect so it writes its token cache, and this package only ever
reads that cache.
"""
import functools
import json
import os
import pathlib
import sys

ENV_SHEET_ID = "TRAINING_LOG_SHEET_ID"
ENV_SA_JSON = "TRAINING_LOG_SA_JSON"
ENV_ATHLETE = "TRAINING_LOG_ATHLETE_JSON"
ENV_DIR = "TRAINING_LOG_DIR"
ENV_GARMIN_TOKENS = "GARMIN_TOKENS_DIR"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

LOG_TAB = "Log"
STATUS_TAB = "Status"


class ConfigError(RuntimeError):
    """A setting is missing or unusable. The message says what to set."""


def sheet_id():
    """The Google spreadsheet key. Required: there is no sensible default."""
    v = os.environ.get(ENV_SHEET_ID, "").strip()
    if not v:
        raise ConfigError(
            f"{ENV_SHEET_ID} is not set. Set it to the spreadsheet key from the "
            f"sheet's URL: docs.google.com/spreadsheets/d/<KEY>/edit")
    return v


def service_account_path():
    """Path to the Google service-account key file. Required, no default."""
    v = os.environ.get(ENV_SA_JSON, "").strip()
    if not v:
        raise ConfigError(
            f"{ENV_SA_JSON} is not set. Set it to the path of your Google "
            f"service-account key file, and share the spreadsheet with that "
            f"account's e-mail address so it may edit it.")
    if not os.path.exists(v):
        raise ConfigError(f"{ENV_SA_JSON} points at a file that does not exist: {v}")
    return v


def garmin_tokens_dir():
    """garminconnect's OAuth token cache directory."""
    return os.path.expanduser(
        os.environ.get(ENV_GARMIN_TOKENS, "").strip() or "~/.garminconnect")


def log_dir():
    """Directory for run logs and local snapshots. Created on demand."""
    p = pathlib.Path(os.environ.get(ENV_DIR, "").strip() or "logs")
    p.mkdir(parents=True, exist_ok=True)
    return p


def athlete_path():
    return os.environ.get(ENV_ATHLETE, "").strip() or "athlete.json"


class Athlete:
    """The athlete's own constants, read from athlete.json.

    Access is by section so a missing section fails with the name of what is
    missing rather than a bare KeyError deep inside the classifier.
    """

    def __init__(self, data, source="<dict>"):
        self._d = data
        self.source = source

    def section(self, name):
        v = self._d.get(name)
        if not isinstance(v, dict):
            raise ConfigError(
                f"'{name}' is missing from {self.source}. Start from "
                f"athlete.example.json, which has every section filled in.")
        return {k: x for k, x in v.items() if not k.startswith("_")}

    def value(self, section, key):
        s = self.section(section)
        if key not in s:
            raise ConfigError(f"'{section}.{key}' is missing from {self.source}.")
        return s[key]

    @property
    def races(self):
        """[(ISO date, label)] of upcoming checkpoints, chronological."""
        out = []
        for item in self._d.get("races") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append((str(item[0]), str(item[1])))
        return sorted(out)

    @property
    def baselines(self):
        """{'rhr': float, 'resp': float}, the shape the scorers expect."""
        s = self.section("baselines")
        return {"rhr": float(s["resting_hr"]), "resp": float(s["respiration"])}


def load_athlete(path=None):
    """Read athlete.json (or the file given). Raises ConfigError with a hint."""
    p = path or athlete_path()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"No athlete file at '{p}'. Copy athlete.example.json to "
            f"athlete.json and put your own numbers in it, or point "
            f"{ENV_ATHLETE} at your copy.")
    except json.JSONDecodeError as e:
        raise ConfigError(f"'{p}' is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"'{p}' must hold a JSON object.")
    return Athlete(data, source=p)


@functools.lru_cache(maxsize=None)
def _cached(p):
    return load_athlete(p)


def athlete():
    """The athlete constants, cached per path for the life of the process."""
    return _cached(athlete_path())


# --------------------------------------------------------------------------- #
# clients (imported lazily: importing this module must not touch the network)  #
# --------------------------------------------------------------------------- #
def garmin_client():
    """Logged-in Garmin Connect client, from the cached OAuth tokens."""
    from garminconnect import Garmin
    g = Garmin()
    g.login(garmin_tokens_dir())
    return g


def sheet_client():
    """Authorised gspread client, from the service-account key."""
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        service_account_path(), scopes=SCOPES)
    return gspread.authorize(creds)


def spreadsheet():
    """The spreadsheet this package works on."""
    return sheet_client().open_by_key(sheet_id())


def run_cli(main, argv=None):
    """Run a module's main() and turn a setup mistake into one readable line.

    A missing environment variable is not a bug in the program, so it does not
    get a traceback: the message already says exactly what to set.
    """
    try:
        return main(argv)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
