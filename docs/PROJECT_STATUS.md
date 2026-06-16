# 프로젝트 현황 — ANSWER_BOT (Slack 사내 챗봇)

> 최종 업데이트: 2026-06-16

---

## 구현 완료 항목

| 구분 | 내용 | 상태 |
|------|------|------|
| PRD | 제품 요구사항 정의서 (`docs/PRD.md`) | ✅ 완료 |
| 모델 선택 | OpenRouter 무료 모델 최적화 (`docs/MODEL_SELECTION.md`) | ✅ 완료 |
| 백엔드 | Slack Bolt + OpenRouter 연동, RAG 파이프라인 | ✅ 완료 |
| DB | Supabase 연동 (pgvector + NullPool + SSL) | ✅ 완료 |
| UI/UX | Slack Block Kit 메시지 컴포넌트 (`slack_bot/ui/`) | ✅ 완료 |
| 테스트 | 단위 테스트 109개 (100% PASS) | ✅ 완료 |
| 문서 | Supabase 설정 가이드, Slack 앱 설정 가이드, QA 리포트 | ✅ 완료 |

---

## 아키텍처 개요

```
Slack (Socket Mode)
      │
      ▼
event_handler.py   ← 이벤트 수신, 3초 ack 후 스레드 처리
      │
      ├─► classifier.py        ← 메시지 분류 (question/request/chitchat)
      ├─► context_retriever.py ← pgvector RAG 검색 (Supabase)
      ├─► llm_service.py       ← OpenRouter API 호출 (폴백 체인)
      └─► slack_service.py     ← Block Kit 응답 전송
              │
              ▼
        ui/message_blocks.py   ← Block Kit 컴포넌트
        ui/reaction_handler.py ← 이모지 반응 처리
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| Slack SDK | slack-bolt (Socket Mode — HTTPS 엔드포인트 불필요) |
| LLM | OpenRouter API (무료 모델 전용) |
| DB | Supabase (PostgreSQL + pgvector) |
| ORM | SQLAlchemy 2.0 (NullPool) |
| 임베딩 | sentence-transformers (로컬, paraphrase-multilingual-mpnet-base-v2) |
| 스케줄러 | APScheduler (주간 배치 요약) |
| 테스트 | pytest |

---

## 사용 중인 모델 (OpenRouter 무료)

| 용도 | 모델 |
|------|------|
| 질문 분류 | `openai/gpt-oss-20b:free` |
| Q&A 답변 | `nex-agi/nex-n2-pro:free` |
| 요약 | `nex-agi/nex-n2-pro:free` |
| RAG 쿼리 생성 | `openai/gpt-oss-20b:free` |
| 이미지 이해 | `nvidia/nemotron-nano-12b-v2-vl:free` |

---

## 배포 전 필수 작업

### 1. `.env` 설정 완성

루트 `.env`에 아래 항목 추가 필요.

```env
# Supabase Transaction Pooler URL
# Supabase 대시보드 → Project Settings → Database → Transaction Pooler 탭에서 복사
DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres?sslmode=require

# Slack App 토큰 — 발급 절차는 docs/SLACK_SETUP.md 참조
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# 수집 대상 채널 및 폴백 담당자 (선택)
TARGET_CHANNEL_IDS=C0123456789
FALLBACK_MENTION_USER_IDS=U0123456789
```

> 현재 `.env`에는 `DATABASE_PW`만 있음. `DATABASE_URL` 전체 연결 문자열로 변경 필요.

### 2. Supabase 스키마 적용

Supabase SQL Editor에서 `slack_bot/db/migrations/001_initial_schema.sql` 실행.
상세 절차는 `docs/SUPABASE_SETUP.md` 참조.

### 3. 패키지 설치

```bash
pip install -r slack_bot/requirements.txt
```

### 4. 봇 실행

`slack_bot/` 디렉토리 안에서 실행해야 한다 (`import config` 경로 때문).

```bash
# 로컬 (conda 환경)
conda run -n slack_bot --cwd D:\My\SLACK_BOT\slack_bot python main.py

# Railway 배포 설정
# Root Directory: slack_bot
# Start Command:  python main.py
```

---

## QA 결과 요약

- 전체 테스트 **109/109 PASS** (1.25초)
- 상세 내용: `docs/QA_REPORT.md`

| 수준 | 내용 |
|------|------|
| MEDIUM | 테스트 환경 SQLAlchemy 버전 불일치 (환경 문제, 코드 이상 없음) |
| LOW | openai 패키지 버전 불일치 (API 호환 확인됨) |
| LOW | context_retriever 번호 불연속 (빈 항목 건너뛸 시) |

---

## 파일 구조

```
SLACK_BOT/
├── .env                           ← 실제 시크릿 (gitignore됨)
├── .env.example                   ← 템플릿
├── properties.txt                 ← LLM 모델 설정
├── docs/
│   ├── PRD.md
│   ├── MODEL_SELECTION.md
│   ├── BACKEND_ARCHITECTURE.md
│   ├── UI_UX_DESIGN.md
│   ├── SUPABASE_SETUP.md
│   ├── QA_REPORT.md
│   └── PROJECT_STATUS.md
└── slack_bot/
    ├── main.py
    ├── config.py
    ├── handlers/event_handler.py
    ├── services/
    │   ├── classifier.py
    │   ├── llm_service.py
    │   ├── context_retriever.py
    │   ├── slack_service.py
    │   └── summarizer.py
    ├── ui/
    │   ├── message_blocks.py
    │   └── reaction_handler.py
    ├── db/
    │   ├── models.py
    │   └── migrations/001_initial_schema.sql
    ├── utils/
    │   ├── pii_filter.py
    │   └── token_counter.py
    └── tests/
        ├── conftest.py
        ├── test_classifier.py
        ├── test_llm_service.py
        ├── test_context_retriever.py
        ├── test_event_handler.py
        ├── test_pii_filter.py
        └── test_token_counter.py
```
