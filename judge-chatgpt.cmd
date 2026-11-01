@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\check_judge_chatgpt.ps1" %*
exit /b %errorlevel%
