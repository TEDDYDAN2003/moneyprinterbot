@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "BOT=%~1"
if not defined BOT if exist "%USERPROFILE%\Downloads\xauusdm-directional-research\src\bot\hull\terminal.py" set "BOT=%USERPROFILE%\Downloads\xauusdm-directional-research"
if not defined BOT goto standalone
if exist "%BOT%\.venv\Scripts\python.exe" goto botpython
where py >nul 2>&1
if errorlevel 1 goto botfallback
py -3 "%~dp0journal.py" --repo "%BOT%" --open-browser
goto finish
:botfallback
python "%~dp0journal.py" --repo "%BOT%" --open-browser
goto finish
:botpython
"%BOT%\.venv\Scripts\python.exe" "%~dp0journal.py" --repo "%BOT%" --open-browser
goto finish
:standalone
where py >nul 2>&1
if errorlevel 1 goto fallback
py -3 "%~dp0journal.py" --open-browser
goto finish
:fallback
python "%~dp0journal.py" --open-browser
:finish
if errorlevel 1 (
  echo.
  echo The journal did not start. See START-HERE.md for Python and MT5 setup.
  pause
  exit /b 1
)
endlocal
