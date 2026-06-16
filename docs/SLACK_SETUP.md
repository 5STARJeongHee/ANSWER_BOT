# Slack 앱 설정 가이드

> Socket Mode 기반 QNA BOT 앱 생성 및 토큰 발급 절차

---

## 1. 앱 생성 (Manifest 방식)

[api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest** 선택.

워크스페이스를 선택한 뒤 아래 JSON을 그대로 붙여넣는다.

```json
{
    "display_information": {
        "name": "QNA BOT",
        "description": "사내 질문/요청에 자동으로 답변하는 AI 챗봇",
        "background_color": "#2c2d30"
    },
    "features": {
        "bot_user": {
            "display_name": "QNA BOT",
            "always_online": true
        }
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "app_mentions:read",
                "channels:history",
                "channels:read",
                "chat:write",
                "reactions:write",
                "reactions:read",
                "users:read"
            ]
        }
    },
    "settings": {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "message.channels"
            ]
        },
        "org_deploy_enabled": false,
        "socket_mode_enabled": true,
        "is_hosted": false,
        "token_rotation_enabled": false
    }
}
```

---

## 2. Manifest 항목 설명

### Bot 스코프 (OAuth Permissions)

| 스코프 | 용도 |
|--------|------|
| `app_mentions:read` | `@QNA BOT` 멘션 이벤트 수신 |
| `channels:history` | 채널 메시지 기록 읽기 (RAG 컨텍스트 수집) |
| `channels:read` | 채널 정보 조회 |
| `chat:write` | 답변 메시지 전송 |
| `reactions:write` | 처리 중 이모지 반응 추가/삭제 |
| `reactions:read` | 이모지 반응 상태 확인 |
| `users:read` | 사용자 정보 조회 (PII 마스킹용) |

### 이벤트 구독

| 이벤트 | 트리거 조건 |
|--------|------------|
| `app_mention` | 채널에서 `@QNA BOT` 멘션 시 → 즉시 답변 |
| `message.channels` | 공개 채널 메시지 전체 → 배경 컨텍스트 축적 (RAG 학습) |

### 기타 설정

| 항목 | 값 | 이유 |
|------|----|------|
| `socket_mode_enabled` | `true` | HTTPS 엔드포인트 없이 동작. 무료 플랜 / 사내 서버 환경에서 필수 |
| `always_online` | `true` | 봇 상태를 항상 온라인으로 표시 |
| `token_rotation_enabled` | `false` | 토큰 자동 갱신 비활성화 (단순 운영 환경) |

---

## 3. 토큰 발급 순서

앱 생성 완료 후 아래 순서로 3개의 값을 발급한다.

### SLACK_APP_TOKEN (xapp- 로 시작)

1. 앱 설정 페이지 → **Basic Information** 스크롤 다운
2. **App-Level Tokens** → **Generate Token and Scopes**
3. Token Name: `socket-mode-token` (임의 지정)
4. **Add Scope** → `connections:write` 선택
5. **Generate** → 발급된 토큰 복사

### SLACK_BOT_TOKEN (xoxb- 로 시작)

1. 앱 설정 페이지 → **OAuth & Permissions**
2. **Install to Workspace** → 권한 승인
3. 설치 완료 후 표시되는 **Bot User OAuth Token** 복사

### SLACK_SIGNING_SECRET

1. 앱 설정 페이지 → **Basic Information**
2. **App Credentials** 섹션 → **Signing Secret** → **Show** 클릭 후 복사

---

## 4. .env 파일 설정

발급한 값을 루트 `.env`에 추가한다.

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
```

---

## 5. 봇을 채널에 초대

토큰 설정 완료 후 수집 대상 채널에 봇을 초대해야 `message.channels` 이벤트를 수신한다.

```
/invite @QNA BOT
```

초대한 채널 ID를 `.env`의 `TARGET_CHANNEL_IDS`에 추가한다.

```env
TARGET_CHANNEL_IDS=C0123456789,C9876543210
```

채널 ID는 Slack 채널 우클릭 → **채널 세부 정보 보기** → 맨 아래에서 확인할 수 있다.

---

## 6. 동작 확인

봇 실행 후 대상 채널에서 멘션으로 테스트한다.

```
@QNA BOT 안녕하세요, 연차 신청은 어떻게 하나요?
```

정상 동작 시 봇이 👀 이모지 반응을 먼저 달고, 답변 생성 후 메시지를 전송한다.
