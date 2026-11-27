@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Opens the local journal target editor. No scanner or order command.
call "%~dp0Start-Journal.cmd" "%~f1" "%~f2" --targets
exit /b %ERRORLEVEL%
