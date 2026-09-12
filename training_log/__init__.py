# -*- coding: utf-8 -*-
"""Garmin -> Google Sheets training log.

A nightly pipeline that turns a Garmin Connect account into a spreadsheet you
can actually read: one row per day in a `Log` tab, a run classifier, two
derived wellness scores, a Banister fitness-fatigue series, a `Status`
dashboard and a monthly journal view.

Submodules import cleanly without credentials; nothing talks to Garmin or
Google until you call something that needs to.
"""
__version__ = "1.0.0"
__all__ = [
    "config", "log_io", "garmin_fetch", "classify", "wellness", "pmc",
    "refresh", "backfill", "status_panel", "month_view",
]
