@echo off
REM Nightly refresh: fill recent Garmin-derived cells into the Log tab and
REM rebuild the Status dashboard. Register this with Task Scheduler.
REM
REM Copy to daily_log_update.cmd and edit the four paths below.
REM Keep this file pure ASCII: a single non-ASCII character is re-read in the
REM console code page, the parser's line boundaries shift, and a REM line
REM splits so that its own tail is executed as a command. The body still runs,
REM so nobody notices for months.

set PY="C:\path\to\your\python.exe"
set REPO=C:\path\to\garmin-training-log

REM Credentials and athlete constants. Nothing here has a default.
set TRAINING_LOG_SHEET_ID=PUT_YOUR_SPREADSHEET_KEY_HERE
set TRAINING_LOG_SA_JSON=C:\path\to\your\service_account.json
set TRAINING_LOG_ATHLETE_JSON=%REPO%\athlete.json

REM The run log holds health data. Keep it out of any repository.
set TRAINING_LOG_DIR=C:\path\to\your\training\logs
set LOG=%TRAINING_LOG_DIR%\log_update.log
if not exist "%TRAINING_LOG_DIR%" mkdir "%TRAINING_LOG_DIR%"

REM Append-only, rolled at about 2 MB so it cannot grow without bound.
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 2000000 move /y "%LOG%" "%LOG%.1" >nul

cd /d "%REPO%"

REM Fail fast on each step so the task's exit code reflects the failure and
REM Task Scheduler's retry actually retries. Without this a later step's
REM success masks an earlier failure and the whole task reports 0, so a
REM transient network drop never gets a second attempt.
%PY% -m training_log.refresh --days 10 >> "%LOG%" 2>&1
if errorlevel 1 exit /b 1

%PY% -m training_log.status_panel >> "%LOG%" 2>&1
if errorlevel 1 exit /b 1

REM Back up the month tabs' hand-written columns. The plan column is the only
REM address of confirmed races and events, it is not in Log, and rebuilding a
REM tab cannot restore it -- without a copy on disk there is no recovery path.
REM Read-only against the sheet, runs last, and fails the task on error: a
REM backup that dies quietly is the exact failure it exists to prevent.
%PY% -m training_log.dump_hand_columns >> "%LOG%" 2>&1
if errorlevel 1 exit /b 1
