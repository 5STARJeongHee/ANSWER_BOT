@echo off
taskkill /F /FI "WINDOWTITLE eq QNA BOT" /IM python.exe 2>nul
tasklist /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq QNA BOT" | find /I "python.exe" >nul
if errorlevel 1 (
    echo QNA BOT이 종료되었습니다.
) else (
    echo 프로세스를 찾지 못했습니다. 수동으로 종료해주세요.
)
