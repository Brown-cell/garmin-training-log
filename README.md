# garmin-training-log

A nightly job that turns a Garmin Connect account into a training log you can
actually read: one row per day in a Google Sheet, sessions classified from lap
structure rather than from pace alone, two derived wellness scores, a Banister
fitness-fatigue series, and two human-facing tabs built on top.

Written by a middle-distance runner who wanted a log that fills itself, and who
kept filling it in wrong by hand.

```
pip install -r requirements.txt
cp athlete.example.json athlete.json        # then edit it
python -m training_log.backfill --create-tab
python -m training_log.backfill --since 2026-01-01
python -m training_log.refresh --days 10
python -m training_log.status_panel
python -m pytest tests/                     # no network, no credentials needed
```

The sheet is in Japanese, because it is a real log that a real person reads
every day. The column names are translated in [Column reference](#column-reference);
nothing else about the pipeline depends on the language.

## What it does

Every night, for each of the last few days:

* pull the day's runs, sleep, HRV, resting heart rate, readiness, steps and
  training load from Garmin Connect;
* work out what session was actually run, from the lap structure plus heart
  rate plus running power;
* score the night's sleep and the body's condition;
* write all of it into a `Log` tab, one row per date, without ever overwriting
  something a human typed;
* rebuild a `Status` dashboard from the whole history.

The monthly view and the hand-column backup are run on demand.

## Architecture

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

`Log` is a database and is not meant to be read. `Status` and the month tabs
are the surfaces. Two tabs, two jobs: the dashboard answers "what should I do
today", the month view answers "what happened this month".

| module | what it owns |
|---|---|
| `config.py` | every environment variable and the athlete constants; **nothing else reads `os.environ`** |
| `log_io.py` | the `Log` schema, date-keyed upsert, and the column policy |
| `garmin_fetch.py` | everything that talks to Garmin, and nothing that judges |
| `classify.py` | laps → session label, menu and result lines |
| `wellness.py` | the sleep score and the condition score (pure functions) |
| `pmc.py` | CTL / ATL / TSB, and what to do about missing days |
| `refresh.py` | the nightly forward update |
| `backfill.py` | the one-off import of history |
| `status_panel.py` | the `Status` dashboard |
| `month_view.py` | the monthly journal tab |
| `verify_month_view.py` | checks a month tab renders without formula errors |
| `dump_hand_columns.py` | backs up the hand-written columns to a local file |
| `check_updated_today.py` | has tonight's job already run? |

## The SAFE rule

This is the part worth stealing. An automated log that can overwrite what you
wrote is worse than no log, because you stop trusting the parts it did not
touch. So every column belongs to exactly one writer, and which set a column is
in is declared in `log_io.py` and never decided at runtime:

| set | columns | policy |
|---|---|---|
| `HAND_COLS` | 試合 (races and events), メモ (note) | never written by the pipeline at all |
| `FILL_EMPTY_COLS` | 種別 (label), 詳細 (menu), データ (result) | written only into an empty cell |
| `GARMIN_COLS` | everything measured | always refreshed; Garmin is authoritative |

Four consequences, each of which exists because the naive version was wrong:

* **Today is never marked as rest**, and neither is a future day.
* **Rest is only written for a completed day the watch has uploaded past.** If
  there is no sync evidence, the cell is left blank and a later run fills it.
  Writing "rest" and moving on turns a sync delay into a permanent lie.
* **A placeholder the pipeline wrote is not a hand label.** If a day was filed
  as rest and running later appears for it, the session columns are re-derived.
  A real hand label is never touched. The same applies to a stale automatic
  jog: machine-format text plus more running in Garmin than that text accounts
  for, which is what an evening second run looks like after a late sync.
* **Re-running is always safe.** Rows are addressed by ISO date, so the
  nightly refresh and a historical backfill can run at the same time.

## Run classifier

The label on a session is decided by three votes, never by pace alone.

**Structure.** Laps are split into reps and sets. A rep is a contiguous run of
"quality" laps — the watch chops one continuous effort into several same-pace
auto-laps, so those are merged back together with their sub-splits kept. Reps
are separated by recovery laps; consecutive reps of similar length form a set.
Rep length, and the ratio of recovery time to work time, is what separates a
lactate-tolerance session from an anaerobic one.

**Heart rate.** The intensity grade comes from the average heart rate of the
reps, against the athlete's own lactate-test anchors in `athlete.json`.
Maximum lap heart rate is display-only: one spike inside a threshold rep would
otherwise promote the whole session.

**Power.** Running power rescues the heart-rate vote, because wrist optical HR
drops out constantly on track reps. A 2:54/km repetition can read 86 bpm on a
day whose race maximum was 172. A session graded on that number is graded on a
sensor failure, so a lap whose power says "hard" is treated as hard.

The gate for "was this lap part of the work" needs the same care. Bare heart
rate cannot gate it: recovery jogs between short repetitions sit in the 170s
while running slower than 5:15/km, and a threshold effort in the heat runs
around 4:15/km at the same heart rate. So the heart-rate clause requires a
pace as well. Without it, a hot-weather threshold run loses its last kilometres
to the pace gate by a handful of seconds and is logged as rest.

**The label is an estimate, and a hand-written one wins.** Correct the cell in
the sheet and the pipeline will not touch it again. There is deliberately no
"something happened here" placeholder: a label that only records uncertainty
cannot be acted on, and it sits in the sheet looking exactly like a human
classification.

## Wellness score

Two numbers, both inferences, both meant to be recalibrated against how you
actually felt.

**Sleep (睡眠点).** Duration, gated hard, plus stage adequacy (deep and REM as
fractions of the night), continuity, and how far overnight HRV recovered
towards the personal band. A short night cannot score well however good the
stages are — which is the whole point, since the stage percentages of a
four-hour night often look excellent.

**Condition (体調点).** Is the body intact: illness, sleep debt, autonomic
disturbance. It starts at 100 and subtracts four independent deductions —
autonomic (HRV against Garmin's personal band), sleep (last night plus
accumulated debt), somatic (resting heart rate, respiration, daytime rest),
and secondary (stress load, overnight recharge).

Two properties are the design:

1. **Recent training excuses only the autonomic term.** A hard session does
   lower HRV, and that is productive fatigue rather than illness, so the
   autonomic deduction shrinks in proportion to how well recent load explains
   the drop. Training does not explain a short night, so the sleep terms are
   never forgiven. An earlier version forgave everything at once and could not
   leave its top label while the excuse was active — including during a week
   of actual illness.
2. **The excuse is continuous, not a switch.** A boolean gate puts a cliff
   between "one jog forgives everything" and "nothing forgives anything", and
   which side of the cliff a day falls on moves the score by tens of points.
   The strength of each recent day is scaled between an easy and a hard bound
   and weighted by how long ago it was, and the window takes the maximum, not
   the sum: three easy jogs are not one hard day.

**This score is not "how hard can I train today".** Training fatigue is what
the fitness-fatigue model and Garmin's own readiness measure. The two disagree
most on the day after a hard session, and that is by design. Read them side by
side: condition for whether the body is healthy, TSB and readiness for whether
it is fresh.

Missing inputs are not bad inputs. A deduction-based score with no data reads
as a perfect day, so a day missing both main channels is not scored at all, and
a day missing a minor channel is marked with a trailing `*`.

## Fitness-fatigue model

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
against a published threshold is not.

**Do not judge on TSB alone.** Every absolute cutoff in this project is an
inference awaiting personal calibration, TSB after a maximal effort is one of
five metrics that all measure the same autonomic recovery and are therefore
wrong together, and the series is only as good as its inputs — which is the
next point.

**A missing day is not a rest day.** Both averages decay towards whatever they
are fed, so scoring an un-synced day as zero makes fatigue fall faster than it
really did. Days with no evidence of import are charged at the current CTL
(assumed typical, which leaves CTL unchanged by definition) and returned in a
separate set. If any of them falls inside the seven-day fatigue window, the
dashboard prints "judgement withheld" instead of a verdict.

Evidence of import has to be a column that is positive when real and empty
otherwise — steps, sleep hours, load. Columns where zero is meaningful (stairs
climbed, battery drained, intensity minutes) cannot serve: a day holding
nothing but zeroes in them looks synced and rested when it is neither.

## Monthly view

`month_view.py` builds one tab per month: a KPI strip, one row per day, a
subtotal after every Sunday, and two columns the athlete writes in. Every other
cell is a formula reading `Log`, so the nightly refresh keeps the month live
without rebuilding it.

* **One hue, and no cell shading.** A single-hue ramp only supports a few
  distinguishable lightness steps, and shading every number turns the month
  into wallpaper. Exceptions are marked with bold navy text instead, so the few
  marks that appear mean something.
* **Only decision columns are marked** — sleep, condition, ACWR, quality
  sessions. Raw physiology sits in a collapsed column group.
* **HRV gets an arrow only when it moves beyond one standard deviation** of its
  own preceding 28 days. An arrow on every wobble is the same as no arrow.
  The arrow is a statistical mark, not a health verdict: the condition score
  judges HRV against Garmin's personal band, which is a different threshold, so
  a day can lose condition points with no arrow next to it.
* **Creating a tab needs `--allow-new-tab`.** A missing tab is nearly always a
  mistyped name, and silently creating one splits the hand columns across a
  real tab and a decoy nobody opens.

`verify_month_view.py` then checks the evaluated grid: one row per day, no
`#ERROR!` anywhere, numeric KPIs, multi-line content on quality days, and the
five conditional-format rules present.

## Status dashboard

`status_panel.py` rebuilds a single screen from the whole log, conclusion
first: the verdict and this week's load target, then the current numbers, then
twelve weeks, then the year by month. Everything is computed in Python and
written as static values — no live formulas, so there is no formula-error
surface at all.

Three things it does on purpose:

* **It refuses to judge on incomplete data** (above).
* **It leads with deltas, not absolutes,** for the race predictions. Garmin's
  estimates can sit a long way from what an athlete actually runs, while their
  movement over four weeks is still informative.
* **It names the band that is doing the judging.** HRV is shown against
  Garmin's personal band (which the condition score uses) *and* against the
  log's own 28-day spread, labelled as such, because the two disagree and a
  screen showing only the second one reads the same number backwards.

After a maximal effort it also prints a warning for a configurable window:
TSB, readiness, HRV, resting heart rate and the condition score are five
measurements of one recovery, so in those days they agree with each other and
are wrong together. Five green lights on one morning are one green light.

## Setup

**1. A spreadsheet.** Create one. Take its key from the URL:
`docs.google.com/spreadsheets/d/<KEY>/edit`.

**2. A service account.** In Google Cloud, enable the Sheets API, create a
service account, download its JSON key — then **share the spreadsheet with the
service account's e-mail address**, giving it edit access. This is the step
everybody forgets; without it every call returns a permission error.

**3. Garmin tokens.** Log in once interactively with
[garminconnect](https://github.com/cyberjunky/python-garminconnect) so it writes
its OAuth token cache (default `~/.garminconnect`). This project only ever
reads that cache; it has no username or password setting.

**4. Environment.** Nothing has a default that could point at the wrong sheet:

| variable | required | meaning |
|---|---|---|
| `TRAINING_LOG_SHEET_ID` | yes | the spreadsheet key |
| `TRAINING_LOG_SA_JSON` | yes | path to the service-account key file |
| `TRAINING_LOG_ATHLETE_JSON` | no | athlete constants (default `./athlete.json`) |
| `TRAINING_LOG_DIR` | no | run logs and snapshots (default `./logs`) |
| `GARMIN_TOKENS_DIR` | no | token cache (default `~/.garminconnect`) |

**5. `athlete.json`.** Copy `athlete.example.json` and put your own numbers in
it. **The values shipped in the example are the author's**, and they are not
defaults in any useful sense: the heart-rate anchors come from a lactate test
done on one person in 2023, the running-power bands were fitted to that
person's hand-labelled sessions, and the condition-score weights were tuned on
roughly two hundred days of that person's data. They are a worked example of
the shape, not a starting point for your physiology. Nothing athlete-specific
is hard-coded anywhere in `training_log/`.

**6. First import.**

```
python -m training_log.backfill --create-tab
python -m training_log.backfill --since 2026-01-01 --sleep 0.5
python -m training_log.status_panel
python -m training_log.month_view --month 7 --write --allow-new-tab
```

`--sleep` throttles the Garmin requests. Several hundred days of history is a
lot of calls to make at full speed against somebody else's service; be polite
about it. The backfill writes one day at a time, so an interrupted run resumes
where it stopped.

**7. Nightly.** Copy `scripts/daily_log_update.example.cmd`, fill in the paths,
and register it with Task Scheduler:

```
schtasks /create /tn TrainingLogRefresh /tr "C:\path\to\daily_log_update.cmd" ^
  /sc daily /st 21:30
```

Two settings are worth opening the task properties for:

* **Run task as soon as possible after a scheduled start is missed.** A laptop
  that was asleep at 21:30 otherwise skips the night entirely, and the next
  night's ten-day window has to heal the gap.
* **Do not stop the task on battery**, and allow a retry or two. A transient
  network failure is the common case and recovers by itself.

The wrapper stops at the first failing step (`exit /b 1`). Without that, a
later step's success masks an earlier failure, the task reports 0, and the
retry never fires. Keep it pure ASCII: one non-ASCII character in a `.cmd` is
re-read in the console code page, the parser's line boundaries shift, and a
`REM` line splits so its own tail is executed as a command — while the body
still runs, so nobody notices for months.

## Column reference

The `Log` tab, left to right. This is the schema; new columns go at the end.

| column | meaning | writer |
|---|---|---|
| `date` | ISO date, the primary key | pipeline |
| `曜日` | weekday | pipeline |
| `試合` | race or event | **hand** |
| `種別` | session label (`Jog`, `Threshold`, `VO2`, `race`, `rest`, `off`, composites) | fill-empty |
| `km` | distance run | Garmin |
| `詳細` | what was done (the menu) | fill-empty |
| `データ` | how it went (splits, HRmax, watts) | fill-empty |
| `睡眠h` `深睡眠h` `レムh` `覚醒h` | sleep, deep, REM, awake, in hours | Garmin |
| `HRV` `RHR` `準備度` | overnight HRV, resting heart rate, readiness | Garmin |
| `睡眠点` `睡眠評価` | sleep score and its label | Garmin |
| `体調点` `体調評価` | condition score and its label | Garmin |
| `歩数` `階段` | steps, floors climbed | Garmin |
| `強度分` | intensity minutes (moderate + 2 × vigorous) | Garmin |
| `BB消費` | Body Battery drained | Garmin |
| `ACWR` | acute:chronic workload ratio | Garmin |
| `回復h` | recovery hours remaining | Garmin |
| `負荷` | session training load | Garmin |
| `メモ` | free note | **hand** |

Labels that appear in the cells:

| label | meaning |
|---|---|
| 体調評価 好調 / 良好 / 要観察 / 不調 | strong / fine, small deduction / watch this / unwell |
| 睡眠評価 優 / 良 / 可 / 不足 | excellent / good / fair / short |
| a trailing `*` | a minor input was unavailable that day |
| a bracket, e.g. `良好(睡眠-12)` | the largest single deduction |
| 種別 `Jog + WS` | easy run with strides |
| 種別 `rest` / `off` | no running / deliberate day off |

The month tabs add `予定` (what was planned or confirmed for that day) and
`内容` (menu and result joined), and reuse the names above for everything else.

## Caveats

* **Every label is an estimate.** The classifier reads laps, not intent. Fix
  the cell and the pipeline leaves it alone from then on.
* **Both scores are inferences**, calibrated on one person. Treat the numbers
  as a series to compare against itself, not as a measurement.
* **An un-synced day is `None`, not zero**, throughout — and it will stay
  blank until the watch uploads. If a day never gets imported at all, the
  dashboard says so rather than averaging it in.
* **Garmin's API is undocumented and moves.** Response shapes differ between
  library versions; several readers here try more than one shape and fall back
  to `None`. A field that quietly becomes empty is the failure mode to expect.
* **`USER_ENTERED` writes eat a leading `+`** and turn `+8` into a formula
  error, which is why every signed number here is written as a plain number
  with the sign supplied by the cell's number format.
* **Merged cells and hand-built tabs do not survive automation.** The `Log`
  schema exists because the original hand-written month tabs had merged cells,
  scores and labels in one cell, and no stable primary key.
* **The month view rewrites its tab**, salvaging the hand columns first. The
  plan column has no other copy anywhere, which is why
  `dump_hand_columns.py` exists and why the nightly job fails loudly if it
  cannot write its snapshot.
* **Rate limits are your problem.** There is no backoff beyond a retry on the
  sheet write; use `--sleep` for anything large.

## License

MIT — see [LICENSE](LICENSE).
