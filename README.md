# garmin-training-log

[![tests](https://github.com/Brown-cell/garmin-training-log/actions/workflows/tests.yml/badge.svg)](https://github.com/Brown-cell/garmin-training-log/actions/workflows/tests.yml)

Keeping a training log by hand means writing down what you did, and a week
later you have stopped. Garmin already holds the laps, the sleep and the heart
rate, so this is a nightly job that writes them into a Google Sheet: one row
per day, the session named from its lap structure, two wellness scores, a
fitness-fatigue series, and two tabs a runner reads in the morning. Written by
a middle-distance runner who kept filling in the log wrong by hand.

The classifier runs without an account. This is `python
examples/classify_demo.py`, verbatim:

```
Session classifier on the fixtures from tests/test_classify.py
(thresholds from athlete.example.json; no network, no credentials)

  in      a rest day (nothing in Garmin)
  kind    rest            quality session: no
  menu    -
  result  -

  in      8 km at 5:00/km, HR 140
  kind    Jog             quality session: no
  menu    8.00km 40min
  result  5:00/km HR140

  in      8 km continuous at 4:00/km, HR 176, 340 W
  kind    Threshold       quality session: yes
  menu    8000m
  result  8000m 32'00" (4'00"-4'00"-4'00"-4'00"-4'00"-4'00"-4'00"-4'00") aHR176 340W

  in      5x1000m at 3:20/km, HR 186, 370 W, 200 m jog recovery
  kind    VO2             quality session: yes
  menu    5×1000m
  result  1000m 3'20"-3'20"-3'20"-3'20"-3'20" HRmax190 370W

  in      8x200m at 2:30/km, HR 170, walk recovery
  kind    Speed           quality session: yes
  menu    8×200m
  result  200m 30-30-30-30-30-30-30-30 HRmax185

  in      a morning jog and an evening threshold run
  kind    Jog + Threshold quality session: yes
  menu    8000m + jog 6.0km
  result  8000m 32'00" (4'00"-4'00"-4'00"-4'00"-4'00"-4'00"-4'00"-4'00") aHR176 340W
          6.0km 5:00/km HR140
```

Every column name, label and note is listed in
[Column reference](#column-reference). They are the sheet's own headers, so
renaming one changes what a human sees and nothing else.

```
pip install -r requirements.txt
cp athlete.example.json athlete.json        # then edit it
python -m training_log.backfill --create-tab
python -m training_log.backfill --since 2026-01-01
python -m training_log.refresh --days 10
python -m training_log.status_panel
python -m pytest tests/                     # no network, no credentials needed
```

## The pipeline

Every night, for each of the last few days, it pulls that day's runs, sleep,
HRV, resting heart rate, readiness, steps and training load from Garmin
Connect, works out what was run, scores the night's sleep and the body's
condition, writes a row into `Log` without touching anything a human typed, and
rebuilds `Status`. The monthly view and the hand-column backup run on demand.

```
Garmin Connect ──► garmin_fetch ──► classify   ─┐
                                    wellness   ─┼─► log_io ──► "Log" tab  (the database,
                                    pmc        ─┘                          one row per day)
                                                                    │
                                          ┌─────────────────────────┴────────────┐
                                          ▼                                      ▼
                                   "Status" tab                            "Jul", "Aug" …
                                   (verdict + numbers,                     (monthly journal,
                                    rebuilt nightly)                        formulas reading Log)
```

`Log` is a database and nobody opens it. `Status` answers "what should I do
today" and the month tabs answer "what happened this month".

| module | what it owns |
|---|---|
| `config.py` | every environment variable and the athlete constants; nothing else reads `os.environ` |
| `log_io.py` | the `Log` schema, date-keyed upsert, and the column policy |
| `garmin_fetch.py` | everything that talks to Garmin, and nothing that judges |
| `classify.py` | laps to session label, menu and result lines |
| `wellness.py` | the sleep score and the condition score (pure functions) |
| `pmc.py` | CTL / ATL / TSB, and what to do about missing days |
| `refresh.py` | the nightly forward update |
| `backfill.py` | the one-off import of history |
| `status_panel.py` | the `Status` dashboard |
| `month_view.py` | the monthly journal tab |
| `verify_month_view.py` | checks a month tab renders without formula errors |
| `dump_hand_columns.py` | backs up the hand-written columns to a local file |
| `check_updated_today.py` | has tonight's job already run? |

## Who owns each column

A log that can overwrite what you wrote is worse than no log, because you stop
trusting the parts it did not touch. **Every column belongs to exactly one
writer**, declared in `log_io.py` and never decided at runtime:

| set | columns | policy |
|---|---|---|
| `HAND_COLS` | `event` (races and events), `note` | never written by the pipeline at all |
| `FILL_EMPTY_COLS` | `kind` (label), `menu`, `result` | written only into an empty cell |
| `GARMIN_COLS` | everything measured | always refreshed; Garmin is authoritative |

Four consequences, each of them replacing an earlier version that got it wrong:

* Today is never marked as rest, and neither is a future day.
* Rest is only written for a completed day the watch has uploaded past. With no
  sync evidence the cell stays blank and a later run fills it; writing "rest"
  and moving on turns a sync delay into a permanent lie.
* A placeholder the pipeline wrote is not a hand label. If running later appears
  on a day filed as rest, the session columns are re-derived; a real hand label
  is never touched. Same for a stale automatic jog, which is machine-format text
  plus more running in Garmin than that text accounts for (an evening second run
  that synced late).
* Re-running is always safe. Rows are addressed by ISO date, so the nightly
  refresh and a historical backfill can run at the same time.

## How a session gets its label

Three votes, never pace alone.

*Structure.* Laps are split into reps and sets. A rep is a contiguous run of
quality laps, since the watch chops one continuous effort into several
same-pace auto-laps and those are merged back with their sub-splits kept. Reps
are separated by recovery laps, and consecutive reps of similar length form a
set. Rep length and the ratio of recovery time to work time separate a
lactate-tolerance session from an anaerobic one.

*Heart rate.* The intensity grade comes from the average heart rate of the
reps, against the athlete's own lactate-test anchors in `athlete.json`. Maximum
lap heart rate is display-only: one spike inside a threshold rep would
otherwise promote the whole session.

*Power.* Running power rescues the heart-rate vote, because wrist optical HR
drops out constantly on track reps. A 2:54/km repetition can read 86 bpm on a
day whose race maximum was 172, and grading that session on 86 bpm is grading
it on a sensor failure, so a lap whose power says "hard" is hard.

The gate for "was this lap part of the work" needs the same care. Recovery jogs
between short repetitions sit in the 170s while running slower than 5:15/km,
and a threshold effort in the heat runs around 4:15/km at the same heart rate,
so the heart-rate clause requires a pace too. Without it a hot-weather
threshold run loses its last kilometres by a handful of seconds and is logged
as rest.

Every label is an estimate and a hand-written one wins: correct the cell and
the pipeline never touches it again. There is no "something happened here"
placeholder, because a label that only records uncertainty cannot be acted on
while looking exactly like a human classification.

## The two wellness scores

Both are inferences, meant to be recalibrated against how you actually felt.

*Sleep (`sleep_score`).* Duration, gated hard, plus stage adequacy (deep and REM as
fractions of the night), continuity, and how far overnight HRV recovered
towards the personal band. A short night cannot score well however good the
stages are, which is the point: the stage percentages of a four-hour night
often look excellent.

*Condition (`cond_score`).* Is the body intact: illness, sleep debt, autonomic
disturbance. It starts at 100 and subtracts four independent deductions,
autonomic (HRV against Garmin's personal band), sleep (last night plus
accumulated debt), somatic (resting heart rate, respiration, daytime rest) and
secondary (stress load, overnight recharge). Two properties are the design:

1. Recent training excuses only the autonomic term. A hard session lowers HRV,
   and that is productive fatigue rather than illness, so the autonomic
   deduction shrinks in proportion to how well recent load explains the drop.
   Training does not explain a short night, so the sleep terms are never
   forgiven. An earlier version forgave everything at once and could not leave
   its top label through a week of real illness.
2. The excuse is continuous rather than a switch, because a boolean gate puts a
   cliff between "one jog forgives everything" and "nothing forgives anything"
   and moves the score by tens of points depending on which side a day lands.
   Each recent day is scaled between an easy and a hard bound and weighted by
   how long ago it was, and the window takes the maximum rather than the sum:
   three easy jogs are not one hard day.

The score does not answer "how hard can I train today"; that is what the
fitness-fatigue model and Garmin's readiness measure, and the two disagree most
on the day after a hard session, by design. Condition says whether the body is
healthy, TSB and readiness whether it is fresh.

Missing inputs are not bad inputs. A deduction-based score with no data reads
as a perfect day, so a day missing both main channels is not scored at all, and
a day missing a minor channel is marked with a trailing `*`.

## CTL, ATL and TSB

A Banister impulse-response model of the kind popularised by the TrainingPeaks
Performance Management Chart:

```
CTL  chronic training load, 42-day exponential mean   "fitness"
ATL  acute training load,    7-day exponential mean   "fatigue"
TSB  CTL - ATL                                        "form"

x_today = x_yesterday + (load_today - x_yesterday) * (1 - exp(-1/days))
```

Garmin's session load is unitless but internally consistent, so comparing a
value against the same athlete's own history is meaningful while comparing it
against a published threshold is not. Every absolute cutoff here awaits
personal calibration, and TSB after a maximal effort is one of five metrics
that measure the same autonomic recovery and are wrong together.

**A missing day is not a rest day.** Both averages decay towards whatever they
are fed, so scoring an un-synced day as zero makes fatigue fall faster than it
really did. Days with no evidence of import are charged at the current CTL
(assumed typical, which leaves CTL unchanged by definition) and returned in a
separate set; if any falls inside the seven-day fatigue window, the dashboard
prints "judgement withheld" instead of a verdict.

Evidence of import has to be a column that is positive when real and empty
otherwise: steps, sleep hours, load. Columns where zero is meaningful (stairs
climbed, battery drained, intensity minutes) cannot serve, since a day holding
nothing but zeroes in them looks synced and rested when it is neither.

## The two tabs you actually read

`status_panel.py` rebuilds one screen from the whole log, conclusion first: the
verdict and this week's load target, then the current numbers, twelve weeks,
and the year by month. It is all computed in Python and written as static
values, so there is no formula-error surface. It refuses to judge on incomplete
data (above); it leads with deltas rather than absolutes for the race
predictions, since Garmin's estimates can sit far from what an athlete actually
runs while their four-week movement is still informative; and it names the band
doing the judging, showing HRV against Garmin's personal band (which the
condition score uses) and against the log's own 28-day spread, because the two
disagree.

After a maximal effort it prints a warning for a configurable window. TSB,
readiness, HRV, resting heart rate and the condition score are five
measurements of one recovery, so in those days they agree with each other and
are wrong together. Five green lights on one morning are one green light.

`month_view.py` builds one tab per month: a KPI strip, one row per day, a
subtotal after every Sunday, and two columns the athlete writes in. Every other
cell is a formula reading `Log`, so the nightly refresh keeps the month live.

* One hue, no cell shading. A single-hue ramp supports only a few
  distinguishable lightness steps, and shading every number turns the month
  into wallpaper, so exceptions get bold navy text instead.
* Only decision columns are marked: sleep, condition, ACWR, quality sessions.
  Raw physiology sits in a collapsed column group.
* HRV gets an arrow only when it moves beyond one standard deviation of its own
  preceding 28 days, since an arrow on every wobble is the same as no arrow. It
  is a statistical mark rather than a health verdict: the condition score judges
  HRV against Garmin's personal band, a different threshold, so a day can lose
  condition points with no arrow.
* Creating a tab needs `--allow-new-tab`. A missing tab is nearly always a
  mistyped name, and silently creating one splits the hand columns across a real
  tab and a decoy nobody opens.

`verify_month_view.py` then checks the evaluated grid: one row per day, no
`#ERROR!` anywhere, numeric KPIs, multi-line content on quality days, and the
five conditional-format rules present.

## Setup

1. A spreadsheet. Create one and take its key from the URL,
   `docs.google.com/spreadsheets/d/<KEY>/edit`.
2. A service account. In Google Cloud, enable the Sheets API, create a service
   account, download its JSON key, then share the spreadsheet with the service
   account's e-mail address, with edit access. This is the step everybody
   forgets; without it every call returns a permission error.
3. Garmin tokens. Log in once interactively with
   [garminconnect](https://github.com/cyberjunky/python-garminconnect) so it
   writes its OAuth token cache (default `~/.garminconnect`). This project only
   reads that cache; it has no username or password setting.
4. `athlete.json`. Copy `athlete.example.json` and put your own numbers in it.
   The values shipped in the example are the author's, not defaults in any
   useful sense: the heart-rate anchors come from a lactate test done on one
   person in 2023, the running-power bands were fitted to that person's
   hand-labelled sessions, and the condition-score weights were tuned on roughly
   two hundred days of that person's data. Nothing athlete-specific is
   hard-coded anywhere in `training_log/`.

No environment variable has a default that could point at the wrong sheet:

| variable | required | meaning |
|---|---|---|
| `TRAINING_LOG_SHEET_ID` | yes | the spreadsheet key |
| `TRAINING_LOG_SA_JSON` | yes | path to the service-account key file |
| `TRAINING_LOG_ATHLETE_JSON` | no | athlete constants (default `./athlete.json`) |
| `TRAINING_LOG_DIR` | no | run logs and snapshots (default `./logs`) |
| `GARMIN_TOKENS_DIR` | no | token cache (default `~/.garminconnect`) |

Then import the history:

```
python -m training_log.backfill --create-tab
python -m training_log.backfill --since 2026-01-01 --sleep 0.5
python -m training_log.status_panel
python -m training_log.month_view --month 7 --write --allow-new-tab
```

`--sleep` throttles the Garmin requests; several hundred days of history is a
lot of calls to make at full speed against somebody else's service. The
backfill writes one day at a time, so an interrupted run resumes where it
stopped.

For the nightly run, copy `scripts/daily_log_update.example.cmd`, fill in the
paths, and register it with Task Scheduler:

```
schtasks /create /tn TrainingLogRefresh /tr "C:\path\to\daily_log_update.cmd" ^
  /sc daily /st 21:30
```

Two settings are worth opening the task properties for. *Run task as soon as
possible after a scheduled start is missed*, since a laptop asleep at 21:30
otherwise skips the night and the next night's ten-day window has to heal the
gap. And *do not stop the task on battery*, with a retry or two, since a
transient network failure is the common case and recovers by itself.

The wrapper stops at the first failing step (`exit /b 1`); without that a later
step's success masks an earlier failure, the task reports 0, and the retry
never fires. Keep it pure ASCII: one non-ASCII character in a `.cmd` is re-read
in the console code page, the parser's line boundaries shift, and a `REM` line
splits so its own tail is executed as a command, while the body still runs, so
nobody notices for months.

## Column reference

The `Log` tab, left to right. This is the schema; new columns go at the end.

| column | meaning | writer |
|---|---|---|
| `date` | ISO date, the primary key | pipeline |
| `wd` | weekday | pipeline |
| `event` | race or event | hand |
| `kind` | session label (`Jog`, `Threshold`, `VO2`, `race`, `rest`, `off`, composites) | fill-empty |
| `km` | distance run | Garmin |
| `menu` | what was done | fill-empty |
| `result` | how it went (splits, HRmax, watts) | fill-empty |
| `sleep_h` `deep_h` `rem_h` `awake_h` | sleep, deep, REM, awake, in hours | Garmin |
| `HRV` `RHR` `readiness` | overnight HRV, resting heart rate, readiness | Garmin |
| `sleep_score` `sleep_label` | sleep score and its label | Garmin |
| `cond_score` `cond_label` | condition score and its label | Garmin |
| `steps` `floors` | steps, floors climbed | Garmin |
| `intensity_min` | intensity minutes (moderate + 2 × vigorous) | Garmin |
| `bb_drained` | Body Battery drained | Garmin |
| `ACWR` | acute:chronic workload ratio | Garmin |
| `recovery_h` | recovery hours remaining | Garmin |
| `load` | session training load | Garmin |
| `note` | free note | hand |

Labels that appear in the cells:

| label | meaning |
|---|---|
| `cond_label` `strong` / `fine` / `watch` / `unwell` | nothing wrong / a small deduction / something is off / several things are |
| `sleep_label` `excellent` / `good` / `fair` / `short` | the night, graded on duration first |
| a trailing `*` | a minor input was unavailable that day |
| a bracket, e.g. `fine(sleep-12)` | the largest single deduction |
| `kind` `Jog + WS` | easy run with strides |
| `kind` `rest` / `off` | no running / a planned day off |

The month tabs add `plan` (what was planned or confirmed for that day) and
`detail` (menu and result joined), and reuse the names above for everything
else.

## Caveats

* Every label is an estimate. The classifier reads laps, not intent. Fix the
  cell and the pipeline leaves it alone from then on.
* Both scores are inferences calibrated on one person. Treat them as a series
  to compare against itself rather than as a measurement.
* An un-synced day is `None` rather than zero throughout, and stays blank until
  the watch uploads. A day that never gets imported is reported as such rather
  than averaged in.
* Garmin's API is undocumented and moves. Response shapes differ between
  library versions; several readers try more than one shape and fall back to
  `None`. A field that quietly becomes empty is the failure mode to expect.
* `USER_ENTERED` writes eat a leading `+` and turn `+8` into a formula error,
  which is why every signed number is written plain with the sign supplied by
  the cell's number format.
* Merged cells and hand-built tabs do not survive automation. The `Log` schema
  exists because the original hand-written month tabs had merged cells, scores
  and labels in one cell, and no stable primary key.
* The month view rewrites its tab, salvaging the hand columns first. The plan
  column has no other copy anywhere, which is why `dump_hand_columns.py` exists
  and why the nightly job fails loudly if it cannot write its snapshot.
* Rate limits are your problem. There is no backoff beyond a retry on the sheet
  write; use `--sleep` for anything large.

## License

MIT, see [LICENSE](LICENSE).
