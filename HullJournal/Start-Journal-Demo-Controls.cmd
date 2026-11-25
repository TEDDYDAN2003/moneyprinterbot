@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Enables the local control panel only. Never arms or selects symbols.
call "%~dp0Start-Journal.cmd" "%~1" "%~2" --demo-controls
exit /b %errorlevel%
