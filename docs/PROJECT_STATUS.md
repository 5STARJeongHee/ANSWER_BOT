# 프로젝트 현황 — QNA BOT (Slack 사내 챗봇)

> 최종 업데이트: 2026-06-29

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
| 파일 처리 | xlsx/docx/pdf/txt 등 첨부파일 텍스트 추출 후 RAG 포함 | ✅ 완료 |
| 이미지 RAG 정확도 | 이미지 포함 질문에 낮은 임계값 적용 (쿼리 변환 손실 보완) | ✅ 완료 |
| 웹 검색 | DuckDuckGo 보조 검색 (RAG 컨텍스트 보완) | ✅ 완료 |
| 증분 백필 | DB 최솟값 ts 기준 미수집 과거 구간만 채움, 중단 후 재개 지원 | ✅ 완료 |
| 백필 명령어 | `@QNA BOT 백필 [기간]` Slack 명령 실행, 권한 제어 | ✅ 완료 |
| 히스토리 명령어 | `@QNA BOT 히스토리` — 채널 질문 이력 목록 (주제 태그 포함) | ✅ 완료 |
| 대시보드 명령어 | `@QNA BOT 대시보드 [기간]` — 응답 통계 종합 뷰 | ✅ 완료 |
| 소개/도움말 명령어 | `@QNA BOT 소개` / `도움말` — 기능 안내 | ✅ 완료 |
| 스레드 유도 | 채널 최초 질문 시 스레드 대화 유도 팁 표시 | ✅ 완료 |
| 분석 메트릭 수집 | 응답 시간, 입출력 토큰, RAG 유사도, 웹 검색 여부 DB 기록 | ✅ 완료 |
| 피드백 수집 | :thumbsup:/:thumbsdown: 이모지 반응 → 긍정/부정 DB 저장 | ✅ 완료 |
| 대시보드 고급 지표 | 활성 사용자, 피드백 응답률, RAG 평균 유사도, 웹 검색 의존율 | ✅ 완료 |
| Fallback 추적 | 최근 Fallback 트리거 질문 목록 대시보드 표시 | ✅ 완료 |
| DB 초기화 통합 | 분산된 migration SQL 파일 → `db-init/01-init.sql` 단일 스크립트 | ✅ 완료 |
| 주제 태그 Stage 1 | 질문 저장 시 LLM으로 핵심 주제 자동 추출, 히스토리·대시보드 표시 | ✅ 완료 |
| DB | Supabase 연동 (pgvector + NullPool + SSL) | ✅ 완료 |
| UI/UX | Slack Block Kit 메시지 컴포넌트 (`slack_bot/ui/`) | ✅ 완료 |
| 테스트 | 단위 테스트 109개 (100% PASS) | ✅ 완료 |
| 문서 | Supabase 설정 가이드, Slack 앱 설정 가이드, QA 리포트 | ✅ 완료 |
| 제품 담당자 DB 관리 | `product_categories` 테이블 + 봇 명령 (`담당자 설정/삭제/목록`) | ✅ 완료 |
| 알림 관리자 DB 관리 | `bot_settings` 테이블 + 봇 명령 (`알림관리자 추가/삭제/목록`) | ✅ 완료 |
| 피드백 루프 강화 | 긍정 QA 임베딩, 부정 LLM 분류, 사용자 투표 (llm_failure_reason / user_failure_reason) | ✅ 완료 |
| 주제 태그 Stage 2 | LLM 한 번 호출로 자유 텍스트 topic canonical 통합 (`batch/topic_normalizer.py`) | ✅ 완료 |
| 세션 윈도우 임베딩 | 비스레드 채널 메시지 5분 슬라이딩 윈도우 청킹 (`utils/conversation_grouper.py`) | ✅ 완료 |
| 요약 주기 설정 명령 | 봇 명령으로 APScheduler 요약 주기 동적 변경 | ✅ 완료 |
| 정규화 즉시 실행 명령 | `@QNA BOT 정규화 실행`으로 배치 즉시 트리거 (중복 실행 방지) | ✅ 완료 |
| 히스토리 기간 옵션 | `@QNA BOT 히스토리 30일` 등 기간 인자 지원 | ✅ 완료 |
| 제품 키 분류 | conversation_message.product_key 컬럼으로 메시지별 제품 분류 기록 | ✅ 완료 |

---

## 추후 구현 예정 항목

| 항목 | 설명 | 우선순위 |
|------|------|---------|
| **히스토리 그룹핑** | canonical 주제별로 이력을 그룹핑하여 표시. 같은 주제의 과거 Q&A 요약 제공. | 높음 |
| **RAG + 주제 통합** | 정규화된 canonical 주제로 RAG 결과 재순위 또는 보조 필터 적용. | 중간 |
| **같은 주제 과거 Q&A 제시** | 질문 답변 시 같은 canonical 주제의 과거 Q&A 요약 블록을 부가 정보로 제공. | 중간 |
| **모니터링** | Prometheus metrics 또는 Slack 알림 추가 (봇 장애, rate limit 이벤트 알림). | 낮음 |
| **분류기 캐시 분산화** | 인메모리 LRU 256개 → Redis (다중 인스턴스 환경에서 캐시 공유). | 낮음 |
| **Ollama 멀티모달 강화** | Ollama 백엔드에서 qwen2-vl 등 multimodal 모델 안정화. | 낮음 |

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
      ├─► 명령어 라우팅 ─────────────────────────────────────
      │     ├── 소개/도움말       → build_intro_blocks()
      │     ├── 히스토리 [기간]   → get_channel_question_history() → build_history_blocks()
      │     ├── 대시보드 [기간]   → get_dashboard_stats() + get_top_topics() → build_dashboard_blocks()
      │     ├── 백필 [기간]       → backfill_channel() (백그라운드 스레드)
      │     ├── 담당자 설정/삭제/목록 → _handle_owner_command() → product_categories 테이블
      │     ├── 알림관리자 추가/삭제/목록 → _handle_notification_admin_command() → bot_settings 테이블
      │     ├── 요약 주기 설정/확인  → update_summary_schedule() (APScheduler 동적 변경)
      │     └── 정규화 실행       → run_normalize() (백그라운드 스레드, 중복 실행 방지)
      │
      ├─► classifier.py        ← 메시지 분류 (QUESTION/REQUEST/NONE) + extract_topic()
      ├─► image_processor.py   ← Vision 모델 이미지 분석
      ├─► file_processor.py    ← xlsx/docx/pdf/txt 텍스트 추출
      ├─► context_retriever.py ← Advanced RAG (Hybrid Search + Reranking + Thread 청킹)
      ├─► web_search.py        ← DuckDuckGo 보조 검색 (RAG 부족 시)
      ├─► summarizer.py        ← 스레드 문맥 요약
      ├─► llm_service.py       ← OpenRouter/Ollama API 호출 (폴백 체인)
      └─► slack_service.py     ← Block Kit 응답 전송
              │
              ▼
        ui/message_blocks.py   ← Block Kit 컴포넌트
        ui/reaction_handler.py ← 이모지 반응 / 피드백 처리

batch/collector.py        ← 증분 백필 (미수집 과거 구간, 중단 후 재개)
batch/categorizer.py      ← 미처리 메시지 topic/is_question 보정 배치
batch/scheduler.py        ← APScheduler 배치 등록 + 주기 동적 변경
batch/topic_normalizer.py ← 자유 텍스트 topic → canonical 통합 배치 (Stage 2, 구현 완료)
utils/conversation_grouper.py ← 채널 메시지 5분 슬라이딩 윈도우 세션 청킹
```

---

## 데이터베이스 스키마

### conversation_message

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| event_id | VARCHAR(100) UNIQUE | Slack event_ts 기반 중복 방지 |
| channel_id | VARCHAR(20) | Slack 채널 ID |
| thread_ts | VARCHAR(30) | 스레드 루트 ts |
| message_ts | VARCHAR(30) | 메시지 타임스탬프 |
| user_id | VARCHAR(20) | Slack 사용자 ID |
| role | VARCHAR(10) | 'user' 또는 'bot' |
| content | TEXT | PII 마스킹된 메시지 본문 |
| is_question | BOOLEAN | 분류기 판단 결과 |
| is_fallback | BOOLEAN | Fallback 발동 여부 |
| category | VARCHAR(20) | 레거시 — is_question으로 대체됨 |
| response_time_ms | INTEGER | 봇 응답 생성 소요 시간 (ms) |
| prompt_tokens | INTEGER | LLM 입력 추정 토큰 |
| completion_tokens | INTEGER | LLM 출력 추정 토큰 |
| rag_avg_similarity | FLOAT | RAG top-k 평균 유사도 (0~1) |
| used_web_search | BOOLEAN | 웹 검색 보조 사용 여부 |
| topic | VARCHAR(100) | LLM 추출 핵심 주제 태그 (예: "Redis 연결 오류") |
| product_key | VARCHAR(50) | LLM 분류 제품 키 (예: "iruda_backend") |
| created_at | TIMESTAMP | 저장 시각 |

UNIQUE: `(channel_id, message_ts)`

### context_embedding

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| source_message_id | BIGINT FK | conversation_message.id (CASCADE DELETE) |
| chunk_text | TEXT | 임베딩 대상 텍스트 |
| chunk_type | VARCHAR(20) | 'message' 단일 메시지 / 'thread' 스레드 통합 청크 |
| embedding | vector(768) | pgvector 벡터 |
| embedding_json | TEXT | JSON 직렬화 fallback |
| created_at | TIMESTAMP | 저장 시각 |

인덱스: `ivfflat (embedding vector_cosine_ops) WITH (lists=100)`, `gin (chunk_text gin_trgm_ops)`

### message_feedback

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| channel_id | VARCHAR(20) | Slack 채널 ID |
| message_ts | VARCHAR(30) | 봇 답변 ts |
| user_id | VARCHAR(20) | 이모지 반응 사용자 ID |
| reaction | VARCHAR(50) | thumbsup / thumbsdown 등 |
| sentiment | VARCHAR(10) | 'positive' / 'negative' |
| llm_failure_reason | VARCHAR(30) | LLM 분류 실패 원인 (wrong_source / hallucination / out_of_scope / format_issue) |
| user_failure_reason | VARCHAR(30) | 사용자가 직접 선택한 실패 원인 |
| created_at | TIMESTAMP | 저장 시각 |

### bot_settings

| 컬럼 | 타입 | 설명 |
|------|------|------|
| key | VARCHAR(100) PK | 설정 키 (예: "notification_admins") |
| value | TEXT | JSON 직렬화된 설정 값 |
| updated_at | TIMESTAMP | 마지막 변경 시각 |

### product_categories

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | |
| product_key | VARCHAR(50) UNIQUE | 제품 키 (예: "iruda_backend") |
| display_name | VARCHAR(100) | 사용자에게 보이는 제품명 |
| owner_user_ids_json | TEXT | 담당자 Slack ID 목록 (JSON 배열) |
| aliases_json | TEXT | 제품 별칭 목록 (JSON 배열, 분류기 힌트용) |
| question_count | INTEGER | 누적 질문 수 (알림 임계값 판단용) |
| notified_at | TIMESTAMP | 마지막 미담당 알림 전송 시각 |
| created_at / updated_at | TIMESTAMP | 생성·수정 시각 |

### context_summary

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| channel_id | VARCHAR(20) | Slack 채널 ID |
| period_start | DATE | 요약 기간 시작 |
| period_end | DATE | 요약 기간 종료 |
| summary_text | TEXT | LLM 생성 요약 |
| created_at | TIMESTAMP | 저장 시각 |

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| Slack SDK | slack-bolt (Socket Mode — HTTPS 엔드포인트 불필요) |
| LLM | OpenRouter API (무료 모델) 또는 로컬 Ollama (`LLM_BACKEND` 선택) |
| DB | Supabase (PostgreSQL + pgvector) 또는 로컬 PostgreSQL |
| ORM | SQLAlchemy (NullPool) |
| 임베딩 | fastembed (ONNX, paraphrase-multilingual-mpnet-base-v2, 768차원) |
| Reranking | fastembed TextCrossEncoder (BAAI/bge-reranker-base) |
| 웹 검색 | duckduckgo-search (API 키 불필요) |
| 스케줄러 | APScheduler (주간 배치 요약) |
| 테스트 | pytest |

---

## 사용 중인 모델 (OpenRouter 무료)

| 용도 | 모델 |
|------|------|
| 질문 분류 + 주제 추출 | `openai/gpt-oss-20b:free` |
| Q&A 답변 | `nex-agi/nex-n2-pro:free` |
| 요약 | `nex-agi/nex-n2-pro:free` |
| RAG 쿼리 생성 | `openai/gpt-oss-20b:free` |
| 이미지 이해 | `nvidia/nemotron-nano-12b-v2-vl:free` |

---

## 배포 전 필수 작업

### 1. `.env` 설정 완성

```env
# DB 연결 (Supabase Transaction Pooler URL 또는 로컬)
DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres?sslmode=require

# Slack App 토큰 — 발급 절차는 docs/SLACK_SETUP.md 참조
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# 수집 대상 채널 및 폴백 담당자 (선택)
TARGET_CHANNEL_IDS=C0123456789
FALLBACK_MENTION_USER_IDS=U0123456789
```

### 2. DB 초기화

Docker 사용 시 `db-init/01-init.sql`이 첫 기동 시 자동 실행됩니다.

Supabase 사용 시 SQL Editor에서 `db-init/01-init.sql` 내용을 직접 실행합니다.
상세 절차는 `docs/SUPABASE_SETUP.md` 참조.

### 3. 봇 실행

```bash
# Docker (권장)
docker compose up -d

# 로컬 (conda 환경)
conda run -n slack_bot --cwd D:\My\SLACK_BOT\slack_bot python main.py
```

---

## QA 결과 요약

- 전체 테스트 **109/109 PASS** (1.25초)
- 상세 내용: `docs/QA_REPORT.md`

---

## 파일 구조

```
SLACK_BOT/
├── .env                           ← 실제 시크릿 (gitignore됨)
├── .env.example                   ← 템플릿
├── properties.yml                 ← LLM 모델 설정 (YAML)
├── db-init/
│   └── 01-init.sql                ← 전체 스키마 초기화 스크립트
├── docs/
│   ├── PRD.md
│   ├── MODEL_SELECTION.md
│   ├── BACKEND_ARCHITECTURE.md
│   ├── UI_UX_DESIGN.md
│   ├── SUPABASE_SETUP.md
│   ├── SLACK_SETUP.md
│   ├── QA_REPORT.md
│   └── PROJECT_STATUS.md          ← 이 파일
└── slack_bot/
    ├── main.py
    ├── config.py
    ├── handlers/event_handler.py  ← 이벤트 + 명령어 라우팅
    ├── services/
    │   ├── classifier.py          ← 분류 + extract_topic()
    │   ├── llm_service.py
    │   ├── context_retriever.py   ← Advanced RAG
    │   ├── slack_service.py
    │   ├── summarizer.py
    │   └── web_search.py
    ├── ui/
    │   ├── message_blocks.py      ← 답변·히스토리·대시보드 블록
    │   └── reaction_handler.py
    ├── db/
    │   ├── models.py
    │   └── repository.py
    ├── batch/
    │   ├── collector.py
    │   ├── categorizer.py         ← topic/is_question 보정 배치
    │   ├── scheduler.py
    │   └── topic_normalizer.py    ← canonical 주제명 통합 배치 (Stage 2)
    └── utils/
        ├── conversation_grouper.py ← 5분 슬라이딩 윈도우 세션 청킹
        ├── file_processor.py      ← xlsx/docx/pdf/txt 추출
        ├── image_processor.py
        ├── pii_filter.py
        └── token_counter.py
```
