# QNA BOT 설치 및 Windows 작업 스케줄러 등록 스크립트
# PowerShell에서 관리자 권한으로 실행: Right-click → "관리자로 실행"

param(
    [string]$StartTime = "09:00",
    [string]$StopTime  = "19:00",
    [string]$BotDir    = "D:\My\SLACK_BOT"
)

$ErrorActionPreference = "Stop"
$EnvName   = "slack_bot"
$SlackDir  = "$BotDir\slack_bot"
$CondaExe  = "C:\Users\data\anaconda3\Scripts\conda.exe"

Write-Host "=== QNA BOT 설치 시작 ===" -ForegroundColor Cyan

# 1. .env 파일 존재 확인
if (-not (Test-Path "$BotDir\.env")) {
    Write-Host "[오류] $BotDir\.env 파일이 없습니다." -ForegroundColor Red
    Write-Host "  .env.example 을 복사 후 실제 값을 채워주세요." -ForegroundColor Yellow
    exit 1
}
Write-Host "[OK] .env 파일 확인" -ForegroundColor Green

# 2. conda 환경 생성 (이미 있으면 건너뜀)
Write-Host "`n[1/3] conda 환경 확인 중..." -ForegroundColor Cyan
$envExists = & $CondaExe env list | Select-String $EnvName
if (-not $envExists) {
    Write-Host "  slack_bot 환경 생성 중 (Python 3.11)..."
    & $CondaExe create -n $EnvName python=3.11 -y
} else {
    Write-Host "  [OK] slack_bot 환경 이미 존재" -ForegroundColor Green
}

# 3. 패키지 설치
Write-Host "`n[2/3] 패키지 설치 중 (fastembed 포함, 수 분 소요)..." -ForegroundColor Cyan
& $CondaExe run -n $EnvName pip install -r "$SlackDir\requirements.txt"
Write-Host "  [OK] 패키지 설치 완료" -ForegroundColor Green

# 4. 작업 스케줄러 등록
Write-Host "`n[3/3] Windows 작업 스케줄러 등록 중..." -ForegroundColor Cyan

foreach ($name in @("QNA BOT 시작", "QNA BOT 종료")) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# 시작 작업 (평일)
Register-ScheduledTask `
    -TaskName "QNA BOT 시작" `
    -Action (New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c title QNA BOT && `"$BotDir\start_bot.bat`"" `
        -WorkingDirectory $BotDir) `
    -Trigger (New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $StartTime) `
    -Settings $settings `
    -RunLevel Highest `
    -Force | Out-Null

# 종료 작업 (평일)
Register-ScheduledTask `
    -TaskName "QNA BOT 종료" `
    -Action (New-ScheduledTaskAction -Execute "`"$BotDir\stop_bot.bat`"") `
    -Trigger (New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $StopTime) `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "  [OK] 평일 $StartTime 자동 시작 / $StopTime 자동 종료 등록 완료" -ForegroundColor Green

Write-Host "`n=== 설치 완료 ===" -ForegroundColor Cyan
Write-Host "수동 시작 : $BotDir\start_bot.bat"
Write-Host "수동 종료 : $BotDir\stop_bot.bat"
Write-Host "시간 변경 : .\setup.ps1 -StartTime '08:30' -StopTime '18:30'"
