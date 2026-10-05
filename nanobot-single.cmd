@echo off
rem Run one nanobot bot with its web dashboard, without Adminbot.
rem   nanobot-single.cmd                                  default bot (%USERPROFILE%\.nanobot\config.json)
rem   nanobot-single.cmd --config "path\to\config.json"   another bot, e.g. one created in Adminbot
rem Sets up venv\ on the first run, and refuses to start while Adminbot or one of its bots runs.
rem Opens the dashboard in the browser once it is up; set NANOBOT_NO_BROWSER=1 to skip.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nanobot-single.ps1" %*
