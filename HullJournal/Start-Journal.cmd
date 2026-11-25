@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "BOT=%~f1"
set "OUT=%~f2"
if "%~1"=="" set "BOT="
if "%~2"=="" set "OUT="
set "OPT="
if /i "%~3"=="--demo-controls" set "OPT=--demo-controls"
if not "%~3"=="" if not defined OPT exit /b 2
cd /d "%~dp0"
if not defined BOT if exist "%~dp0..\src\bot\hull\terminal.py" set "BOT=%~dp0.."
if defined OPT if not defined BOT goto controlsneedrepo
if not defined BOT if exist "%USERPROFILE%\Downloads\xauusdm-directional-research\src\bot\hull\terminal.py" set "BOT=%USERPROFILE%\Downloads\xauusdm-directional-research"
if defined OUT goto selected
set "OUT=%~dp0data"
if not defined BOT goto selected
for %%I in ("%BOT%\HullJournal\data") do set "PREVIOUS=%%~fI"
if /I "%PREVIOUS%"=="%OUT%" goto selected
if not exist "%PREVIOUS%\journal.sqlite3" goto selected
if exist "%OUT%\journal.sqlite3" (
  echo Two different journal checkpoints exist. Pass your existing data folder as the second argument.
  pause
  exit /b 2
)
if not exist "%OUT%\tickets.csv" if not exist "%OUT%\setups.csv" set "OUT=%PREVIOUS%"
:selected
if defined OPT if not exist "%BOT%\.venv\Scripts\python.exe" goto controlsneedrepo
if not defined BOT goto standalone
if exist "%BOT%\.venv\Scripts\python.exe" goto botpython
where py >nul 2>&1
if errorlevel 1 goto botfallback
py -3 "%~dp0journal.py" --repo "%BOT%" --out "%OUT%" --open-browser %OPT%
goto finish
:botfallback
python "%~dp0journal.py" --repo "%BOT%" --out "%OUT%" --open-browser %OPT%
goto finish
:botpython
"%BOT%\.venv\Scripts\python.exe" "%~dp0journal.py" --repo "%BOT%" --out "%OUT%" --open-browser %OPT%
goto finish
:standalone
where py >nul 2>&1
if errorlevel 1 goto fallback
py -3 "%~dp0journal.py" --out "%OUT%" --open-browser %OPT%
goto finish
:fallback
python "%~dp0journal.py" --out "%OUT%" --open-browser %OPT%
:finish
if errorlevel 1 (
  echo.
  echo The journal did not start. See START-HERE.md for Python and MT5 setup.
  pause
  exit /b 1
)
endlocal
exit /b 0
:controlsneedrepo
echo Demo controls require the full updated bot folder and its .venv. No scanner started.
echo Open this launcher in that checkout, or give the bot folder as the first argument.
pause
exit /b 2
