@echo off
where docker >nul 2>&1
if errorlevel 1 (
    echo Docker not found.
    exit /b 1
)
cd /d "D:\My\SLACK_BOT"
docker compose stop
echo QNA BOT stopped.
