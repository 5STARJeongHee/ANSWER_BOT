@echo off
where docker >nul 2>&1
if errorlevel 1 (
    echo Docker not found. Install Docker Desktop first.
    echo https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)
cd /d "D:\My\SLACK_BOT"
docker compose up -d
echo QNA BOT started. Run: docker compose logs -f
