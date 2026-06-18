# 프로젝트 현황 — ANSWER_BOT (Slack 사내 챗봇)

> 최종 업데이트: 2026-06-18

---

## 구현 완료 항목

| 구분 | 내용 | 상태 |
|------|------|------|
| PRD | 제품 요구사항 정의서 (`docs/PRD.md`) | ✅ 완료 |
| 모델 선택 | OpenRouter 무료 모델 최적화 (`docs/MODEL_SELECTION.md`) | ✅ 완료 |
| 백엔드 | Slack Bolt + OpenRouter 연동, RAG 파이프라인 | ✅ 완료 |
| Advanced RAG | Hybrid Search + Cross-Encoder Reranking + Thread 청킹 | ✅ 완료 |
| DM 핸들러 | 1:1 DM 수신 및 RAG 기반 자동 답변 | ✅ 완료 |
| 이미지 분석 | Vision 모델로 첨부 이미지 분석 후 컨텍스트 포함 | ✅ 완료 |
| 웹 검색 | DuckDuckGo 보조 검색 (RAG 컨텍스트 보완) | ✅ 완료 |
| 증분 백필 | 마지막 수집 ts 이후만 가져와 중복 방지 | ✅ 완료 |
| DB | Supabase 연동 (pgvector + NullPool + SSL) | ✅ 완료 |
| UI/UX | Slack Block Kit 메시지 컴포넌트 (`slack_bot/ui/`) | ✅ 완료 |
| 테스트 | 단위 테스트 109개 (100% PASS) | ✅ 완료 |
| 문서 | Supabase 설정 가이드, Slack 앱 설정 가이드, QA 리포트 | ✅ 완료 |

---

## 아키텍처 개요

```
Slack (Socket Mode)
      │
      ├── 채널 메시지 / @멘션
      └── DM 메시지 (ENABLE_DM_HANDLER=true)
            │
            ▼
event_handler.py   ← 이벤트 수신, 3초 ack 후 스레드 처리
      │
      ├─► classifier.py        ← 메시지 분류 (question/request/chitchat)
      ├─► image_processor.py   ← Vision 모델 이미지 분석
      ├─► context_retriever.py ← Advanced RAG (Hybrid Search + Reranking + Thread 청킹)
      ├─► web_search.py        ← DuckDuckGo 보조 검색 (RAG 부족 시)
      ├─► summarizer.py        ← 스레드 문맥 요약
      ├─► llm_service.py       ← OpenRouter/Ollama API 호출 (폴백 체인)
      └─► slack_service.py     ← Block Kit 응답 전송
              │
              ▼
        ui/message_blocks.py   ← Block Kit 컴포넌트
        ui/reaction_handler.py ← 이모지 반응 / 피드백 처리

batch/collector.py  ← 증분 백필 (마지막 수집 ts 이후분만 수집)
batch/scheduler.py  ← APScheduler 주간 요약 배치
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| Slack SDK | slack-bolt (Socket Mode — HTTPS 엔드포인트 불필요) |
| LLM | OpenRouter API (무료 모델) 또는 로컬 Ollama (`LLM_BACKEND` 선택) |
| DB | Supabase (PostgreSQL + pgvector) |
| ORM | SQLAlchemy 2.0 (NullPool) |
| 임베딩 | fastembed (ONNX, paraphrase-multilingual-mpnet-base-v2, ~400MB) |
| Reranking | fastembed TextCrossEncoder (BAAI/bge-reranker-base) |
| 웹 검색 | duckduckgo-search (API 키 불필요) |
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
├── properties.yml                 ← LLM 모델 설정 (YAML)
├── docs/
│   ├── PRD.md
│   ├── MODEL_SELECTION.md
│   ├── BACKEND_ARCHITECTURE.md
│   ├── UI_UX_DESIGN.md
│   ├── SUPABASE_SETUP.md
│   ├── SLACK_SETUP.md
│   ├── QA_REPORT.md
│   └── PROJECT_STATUS.md
└── slack_bot/
    ├── main.py
    ├── config.py
    ├── handlers/event_handler.py  ← 채널/DM/멘션 이벤트 통합 처리
    ├── services/
    │   ├── classifier.py
    │   ├── llm_service.py
    │   ├── context_retriever.py   ← Advanced RAG (Hybrid+Rerank+Thread)
    │   ├── slack_service.py
    │   ├── summarizer.py
    │   └── web_search.py          ← DuckDuckGo 보조 검색
    ├── ui/
    │   ├── message_blocks.py
    │   └── reaction_handler.py
    ├── db/
    │   ├── models.py
    │   ├── repository.py
    │   └── migrations/001_initial_schema.sql
    ├── batch/
    │   ├── collector.py           ← 증분 백필
    │   └── scheduler.py
    ├── utils/
    │   ├── image_processor.py     ← Vision 모델 이미지 분석
    │   ├── pii_filter.py
    │   └── token_counter.py
    └── tests/
        ├── conftest.py
        ├── test_classifier.py
        ├── test_llm_service.py
        ├── test_context_retriever.py
        ├── test_event_handler.py
        ├── test_pii_filter.py
        ├── test_token_counter.py
        └── test_web_search.py
```
