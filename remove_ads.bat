@echo off
setlocal
rem Drag converted (XACT3) .xwb files or folders onto this file to remove the
rem short tracks - DJ talk, ads and commercials - and keep only the songs.
rem You get a preview first; nothing is written until you confirm.
rem Results go in a "songs_only" folder next to the originals.

if "%~1"=="" (
    echo Drag converted .xwb files or folders onto remove_ads.bat.
    echo.
    pause
    exit /b 1
)

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

set "SECS=90"
set /p "SECS=Keep tracks at least how many seconds long? [90]: "
set "RATE="
choice /C YN /N /M "Also remove tracks with a lower sample rate than the songs? [Y/N]: "
if errorlevel 2 (set "RATE=") else (set "RATE=--top-rate")

%PY% "%~dp0filter_xwb.py" --dry-run --min-seconds %SECS% %RATE% %*
if errorlevel 1 (
    echo.
    echo Something went wrong - see the messages above.
    pause
    exit /b 1
)
echo.
choice /C YN /N /M "Save the banks with only the KEEP tracks? [Y/N]: "
if errorlevel 2 (
    echo Nothing was written.
    pause
    exit /b 0
)
%PY% "%~dp0filter_xwb.py" --min-seconds %SECS% %RATE% %* >nul
if errorlevel 1 (echo Some banks failed - run again to see the messages.) else (echo Done. The trimmed banks are in the "songs_only" folder.)
pause
