@echo off
rem Same as convert.bat, but writes the XACT3 format used by Techland games
rem (Dead Island, Dead Island Riptide, ...). Drag your .xwb files onto this file.
call "%~dp0convert.bat" --techland %*
