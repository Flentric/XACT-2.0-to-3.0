@echo off
rem Same as convert.bat, but writes the XACT3 format used by Techland games
rem (Dead Island, Dead Island Riptide, ...). Drag your .xwb files onto this file.
rem Xbox 360 banks are converted for PC. Their XMA audio needs vgmstream-cli:
rem put vgmstream-cli.exe (and its DLLs) in this folder. Get it from https://vgmstream.org
call "%~dp0convert.bat" --techland %*
