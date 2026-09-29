@echo off
setlocal
rem Drag one or more XACT2 .xwb files onto this file to convert them.
rem Each converted bank is written next to the original as <name>.xact3.xwb

if "%~1"=="" (
    echo Drag one or more .xwb files onto convert.bat to convert them to XACT 3.
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

%PY% "%~dp0xwb2to3.py" %*
echo.
if errorlevel 1 (echo Some files failed to convert - see the messages above.) else (echo Done.)
pause
