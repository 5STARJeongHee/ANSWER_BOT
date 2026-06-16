@echo off
cd /d D:\My\SLACK_BOT
docker compose up -d
echo QNA BOT (Docker) 시작됨. 로그: docker compose logs -f
