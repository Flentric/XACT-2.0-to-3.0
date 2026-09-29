@echo off
setlocal
rem Drag one or more XACT2 .xwb files onto this file to convert them to XACT 3.
rem Converted banks keep their file name and go in a "converted" folder next
rem to the originals.
rem
rem Targeting a specific game? Also drag any .xwb from that game onto this file
rem (together with yours) and the output will use the game's XACT3 format.
rem For Dead Island / other Techland games just use convert_techland.bat.

set "FIRST=%~1"
if "%FIRST%"=="" goto usage
if "%FIRST:~0,2%"=="--" if "%~2"=="" goto usage

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
if errorlevel 1 (echo Some files failed to convert - see the messages above.) else (echo Done. Converted banks are in the "converted" folder.)
pause
exit /b

:usage
echo Drag one or more .xwb files onto this .bat file to convert them to XACT 3.
echo.
pause
exit /b 1
