# QNA BOT

사내 Slack 채널의 대화를 수집하고, 과거 대화를 RAG(검색 증강 생성)로 활용해 직원 질문에 자동으로 답변하는 챗봇입니다.

## 주요 기능

- **자동 Q&A**: 채널 메시지를 LLM으로 분류해 질문/요청에 자동 답변
- **Advanced RAG**: Hybrid Search(키워드+벡터 결합), Cross-Encoder Reranking, Thread 청킹으로 검색 정밀도 향상
- **DM 응답**: 1:1 다이렉트 메시지도 RAG 기반으로 자동 답변 (`ENABLE_DM_HANDLER=true`)
- **이미지 이해**: 첨부 이미지를 Vision 모델로 분석해 질문 컨텍스트에 포함
- **웹 검색 보완**: RAG 컨텍스트가 부족할 때 DuckDuckGo로 보조 검색 (API 키 불필요)
- **@멘션 응답**: `@QNA BOT 질문내용` 형태로 직접 호출 가능
- **Fallback**: 불확실한 답변은 담당자에게 자동 에스컬레이션
- **PII 필터링**: 개인정보 자동 마스킹 후 저장
- **증분 백필**: 재시작 시 마지막 수집 시점 이후분만 가져와 중복 없이 축적
- **Socket Mode**: 인바운드 포트/URL 불필요, 방화벽 뒤에서도 동작

## 기술 스택

| 항목 | 내용 |
|---|---|
| 언어 | Python 3.11 |
| Slack | Slack Bolt, Socket Mode |
| LLM | OpenRouter API (무료 모델) |
| 임베딩 | fastembed (ONNX, ~50MB) |
| DB | PostgreSQL + pgvector |
| 실행 환경 | Docker Compose 또는 conda |

## 빠른 시작 (Docker)

### 1. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 편집합니다.

```env
# Slack (앱 생성 후 발급 — docs/SLACK_SETUP.md 참고)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# OpenRouter (https://openrouter.ai)
OPENROUTER_API_KEY=sk-or-...

# 수집 대상 채널 ID (쉼표 구분)
TARGET_CHANNEL_IDS=C12345678,C87654321

# PostgreSQL 비밀번호 (docker-compose가 DATABASE_URL 자동 조합)
POSTGRES_PASSWORD=강한패스워드로변경
```

### 2. 빌드 및 실행

```bash
docker compose up -d
```

처음 실행 시 fastembed 모델 다운로드(~400MB)를 포함해 5~10분 소요됩니다.

### 3. 로그 확인

```bash
docker compose logs -f
```

### 시작 / 종료

```bash
docker-start.bat   # 시작
docker-stop.bat    # 종료
```

---

## 환경변수 전체 목록

| 변수 | 필수 | 설명 | 기본값 |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | 봇 OAuth 토큰 | — |
| `SLACK_APP_TOKEN` | ✅ | Socket Mode 앱 토큰 | — |
| `SLACK_SIGNING_SECRET` | ✅ | 앱 서명 시크릿 | — |
| `OPENROUTER_API_KEY` | ✅ | OpenRouter API 키 | — |
| `TARGET_CHANNEL_IDS` | 권장 | 수집 채널 ID 목록 | (빈 값 = 멘션만 처리) |
| `ENABLE_DM_HANDLER` | — | DM 메시지 수집 및 응답 활성화 | `true` |
| `POSTGRES_PASSWORD` | ✅ (Docker) | DB 비밀번호 | — |
| `POSTGRES_USER` | — | DB 유저명 | `slackbot` |
| `POSTGRES_DB` | — | DB 이름 | `slackbot` |
| `DATABASE_URL` | ✅ (conda) | 외부 DB 직접 연결 시 | — |
| `LLM_BACKEND` | — | LLM 백엔드 선택 (`openrouter` / `ollama`) | `openrouter` |
| `ENABLE_VECTOR_SEARCH` | — | pgvector 사용 여부 | `true` |
| `ENABLE_HYBRID_SEARCH` | — | 키워드+벡터 Hybrid Search | `true` |
| `ENABLE_RERANKING` | — | Cross-Encoder Reranking 활성화 | `true` |
| `RERANK_MODEL` | — | Reranker 모델명 | `BAAI/bge-reranker-base` |
| `RAG_RERANK_POOL_K` | — | Reranker 초기 후보 수 | `15` |
| `ENABLE_THREAD_CHUNKING` | — | 스레드 단위 통합 임베딩 | `true` |
| `ENABLE_WEB_SEARCH` | — | RAG 부족 시 웹 검색 보완 | `true` |
| `FALLBACK_MENTION_USER_IDS` | — | 에스컬레이션 담당자 ID | — |
| `LOG_LEVEL` | — | 로그 레벨 | `INFO` |

---

## conda 환경으로 실행 (Windows)

### 최초 설치

```powershell
# 관리자 권한 PowerShell에서 실행
.\setup.ps1
```

conda 환경 생성, 패키지 설치, Windows 작업 스케줄러(평일 09:00~19:00) 등록까지 자동으로 처리합니다.

### 수동 시작 / 종료

```bat
start_bot.bat   :: 시작
stop_bot.bat    :: 종료
```

### conda 환경에서 DATABASE_URL 필요

```env
DATABASE_URL=postgresql://유저:패스워드@호스트:5432/DB이름
```

---

## 과거 대화 백필 (선택)

봇 도입 전 대화를 소급 수집하려면 최초 1회 실행합니다.

```env
RUN_BACKFILL=true   # .env에 추가
```

재시작하면 최근 90일치 대화를 자동 수집합니다. 완료 후에는 삭제하거나 `false`로 되돌립니다.

> **백그라운드 실행**: 백필은 별도 스레드에서 실행되므로 수집이 진행되는 동안에도 봇이 Slack 이벤트를 정상 수신하고 답변합니다.

> **증분 수집 및 재개**: DB에 저장된 가장 오래된 메시지 타임스탬프를 기준으로 아직 수집되지 않은 과거 구간만 채웁니다. 백필 도중 재시작해도 끊긴 곳부터 이어서 수집하며, 이미 완료된 구간은 API 호출 없이 즉시 스킵합니다.

---

## 디렉토리 구조

```
SLACK_BOT/
├── slack_bot/
│   ├── main.py               # 진입점
│   ├── config.py             # 환경변수 로드
│   ├── handlers/
│   │   └── event_handler.py  # 채널/DM/멘션 이벤트 처리
│   ├── services/
│   │   ├── classifier.py     # 메시지 분류 (question/chitchat)
│   │   ├── context_retriever.py  # Advanced RAG (Hybrid+Rerank+Thread)
│   │   ├── llm_service.py    # OpenRouter/Ollama API 호출 (폴백 체인)
│   │   ├── slack_service.py  # Block Kit 응답 전송
│   │   ├── summarizer.py     # 스레드 요약
│   │   └── web_search.py     # DuckDuckGo 웹 검색 보완
│   ├── db/                   # SQLAlchemy 모델 & 레포지토리
│   ├── batch/                # 증분 백필, 요약 스케줄러
│   ├── ui/                   # Block Kit 메시지 빌더
│   └── utils/
│       ├── image_processor.py  # Vision 모델 이미지 분석
│       ├── pii_filter.py       # PII 마스킹
│       └── token_counter.py    # 토큰 예산 관리
├── db-init/
│   └── 01-init.sql           # pgvector 확장 활성화
├── docs/                     # Slack 앱 설정, Supabase 가이드
├── docker-compose.yml
├── docker-start.bat
├── docker-stop.bat
├── start_bot.bat             # conda 실행
├── stop_bot.bat              # conda 종료
├── setup.ps1                 # Windows 초기 설치
├── .env.example
└── properties.yml            # LLM 모델 설정 (YAML)
```

---

## 문서

- [Slack 앱 생성 가이드](docs/SLACK_SETUP.md)
- [Supabase 연결 가이드](docs/SUPABASE_SETUP.md)
- [프로젝트 현황](docs/PROJECT_STATUS.md)
