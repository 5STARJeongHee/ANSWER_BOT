# 사내 Slack Q&A 챗봇 백엔드 구현 설명서

> 작성일: 2026-06-16 / 최종 업데이트: 2026-06-29  
> 구현 언어: Python 3.11+  
> 작업 디렉토리: `D:\My\SLACK_BOT\slack_bot\`

---

## 1. 전체 아키텍처

```
[Slack Workspace]
   │  Socket Mode (WebSocket)
   ▼
[main.py] ─── SocketModeHandler (slack-bolt)
   │
   ├── [handlers/event_handler.py]
   │     ├── @app.event("app_mention")  → ack() 즉시 + threading.Thread 분리
   │     │     ├── 명령어 라우팅 (소개/히스토리/대시보드/백필)
   │     │     └── 일반 질문 → _process_question()
   │     └── @app.event("message")     → ack() 즉시 + threading.Thread 분리
   │           ├── DM: 전체 처리 (rag_channel_id=None)
   │           └── 채널: classify → is_actionable 시 _process_question()
   │
   ├── [services/classifier.py]        → 분류 (QUESTION/REQUEST/NONE) + extract_topic()
   ├── [services/context_retriever.py] → Advanced RAG (Hybrid+Reranking+Thread청킹)
   ├── [services/llm_service.py]       → OpenRouter/Ollama API 호출 + 재시도/fallback
   ├── [services/slack_service.py]     → 메시지 전송, 이력 조회
   ├── [services/summarizer.py]        → 스레드 문맥 요약
   ├── [services/web_search.py]        → DuckDuckGo 보조 검색
   │
   ├── [batch/collector.py]            → 증분 백필 (미수집 과거 구간, 중단 후 재개)
   ├── [batch/categorizer.py]          → 미처리 메시지 topic/is_question 보정 배치
   ├── [batch/scheduler.py]            → APScheduler 배치 등록 + 주기 동적 변경
   ├── [batch/topic_normalizer.py]     → 자유 텍스트 topic canonical 통합 배치 (Stage 2)
   │
   ├── [db/models.py]                  → SQLAlchemy ORM + scoped_session
   ├── [db/repository.py]              → CRUD 함수 (모든 DB 접근 중앙화)
   │
   └── [utils/]
         ├── conversation_grouper.py  → 채널 메시지 5분 슬라이딩 윈도우 세션 청킹
         ├── file_processor.py        → xlsx/docx/pdf/txt 텍스트 추출
         ├── image_processor.py       → Vision 모델 이미지 분석
         ├── pii_filter.py            → 이메일/전화/주민번호 마스킹
         └── token_counter.py         → 한국어 혼합 토큰 추정

[PostgreSQL 15 + pgvector]
   ├── conversation_message           (메시지 원문, 분석 메트릭, 주제 태그, product_key)
   ├── context_embedding              (벡터 768차원, ivfflat + trgm 인덱스)
   ├── message_feedback               (이모지 반응 피드백, llm/user 실패 원인)
   ├── context_summary                (주간 요약본)
   ├── bot_settings                   (알림 관리자 등 봇 설정 key-value)
   └── product_categories             (제품별 담당자 및 질문 카운트)
```

---

## 2. 파일별 역할

| 파일 | 역할 | 핵심 결정사항 |
|------|------|--------------|
| `main.py` | 앱 진입점, 초기화 오케스트레이션 | DB → 핸들러 → 스케줄러 → Socket Mode 순 기동 |
| `config.py` | 환경변수 + properties.yml 로드 | 모델명은 properties.yml 우선, env fallback |
| `db/models.py` | ORM 모델, 엔진/세션 팩토리 | scoped_session으로 스레드 안전 보장 |
| `db/repository.py` | CRUD 함수 중앙화 | 모든 DB 접근은 이 모듈만 사용 |
| `handlers/event_handler.py` | Bolt 이벤트 처리 + 명령어 라우팅 | ack() 즉시 → Thread 분리 패턴 |
| `services/classifier.py` | 메시지 분류 (QUESTION/REQUEST/NONE) + `extract_topic()` | 분류 인메모리 LRU 캐시 256개 |
| `services/llm_service.py` | OpenRouter/Ollama API 통합 | 지수 백오프 + 모델 fallback 체인 |
| `services/context_retriever.py` | Advanced RAG (Hybrid+Reranking+Thread청킹) | pgvector 없으면 텍스트 fallback |
| `services/slack_service.py` | Slack 메시지 전송/조회 | rate limit 재시도, Retry-After 헤더 준수 |
| `services/summarizer.py` | 스레드 문맥 요약 / 주간 대화 요약 배치 | APScheduler 월요일 02시 실행 |
| `services/web_search.py` | DuckDuckGo 보조 검색 | RAG 최고 유사도 0.90 미만일 때만 호출 |
| `batch/collector.py` | 증분 백필 (미수집 과거 구간) | DB 최솟값 ts 기준, 1.3초 rate limit 대응 |
| `batch/categorizer.py` | 미처리 메시지 topic/is_question 보정 배치 | --count/--all/--dry-run/--limit 옵션 지원 |
| `batch/scheduler.py` | APScheduler 등록 + 주기 동적 변경 | `update_summary_schedule()` API 제공 |
| `batch/topic_normalizer.py` | 자유 텍스트 topic canonical 통합 배치 (Stage 2) | LLM 한 번 호출로 distinct topic 그룹핑 |
| `ui/message_blocks.py` | Block Kit 컴포넌트 빌더 | 답변·히스토리·대시보드·소개 블록 |
| `ui/reaction_handler.py` | 이모지 반응 피드백 처리 | reaction_added 이벤트 → message_feedback 저장 |
| `utils/file_processor.py` | 첨부파일 텍스트 추출 | xlsx/docx/pdf/txt 지원, 파일당 3000자 제한 |
| `utils/conversation_grouper.py` | 채널 메시지 5분 슬라이딩 윈도우 세션 청킹 | 비스레드 메시지를 시간 기준으로 그룹핑 |
| `utils/image_processor.py` | Vision 모델 이미지 분석 | base64 직렬화, 동시 처리 상한 제어 |
| `utils/pii_filter.py` | PII 마스킹 | 이메일/전화/주민번호/카드/IP 패턴 |
| `utils/token_counter.py` | 토큰 추정 | 한국어 1.5자/토큰, 영어 1.3단어/토큰 |

---

## 3. 핵심 설계 결정사항

### 3.1 임베딩 모델 선택

**선택: `paraphrase-multilingual-mpnet-base-v2` (차원: 768)**

PRD Q5에서 임베딩 모델이 미확정 상태였으므로 다음 기준으로 선택했다.

- OpenRouter 무료 티어에는 임베딩 전용 모델이 없음 (MODEL_SELECTION.md 확인)
- `paraphrase-multilingual-mpnet-base-v2`: 50개 언어 지원, 한국어 검증됨
- 차원 768이 `ko-sroberta-multitask`(768), `multilingual-e5-large`(1024) 대비 균형점
- DB의 `VECTOR(768)` 상수는 `config.EMBEDDING_DIM = 768`로 중앙 관리

### 3.2 스레드 안전 세션 패턴

Slack Bolt의 이벤트 핸들러는 `threading.Thread`로 LLM 작업을 분리한다. SQLAlchemy 세션은 스레드 간 공유 불가이므로 `scoped_session`을 사용하고, **스레드 내에서 `session_factory()`를 호출**하여 각 스레드가 독립 세션을 갖도록 강제한다.

```python
# 잘못된 패턴 (절대 하지 말 것)
session = get_session()
threading.Thread(target=worker, args=[session]).start()  # 세션 공유 위험

# 올바른 패턴
def worker():
    session = session_factory()  # 스레드 내에서 새 세션 생성
    try:
        ...
    finally:
        session.close()
```

### 3.3 이벤트 중복 처리 방지

두 겹의 방어선을 적용한다.

1. **사전 체크**: `ack()` 직후 스레드 분기 전에 `event_id` 조회
2. **DB unique 제약**: `conversation_message.event_id` UNIQUE 컬럼이 race condition에서도 중복을 차단

Slack이 재전송하는 이벤트(`event_ts` 동일)도 unique constraint로 안전하게 무시된다.

### 3.4 봇 루프 방지

봇 자신의 응답도 Slack 이벤트로 돌아온다. 두 가지 방법으로 필터링한다.

- `event.get("bot_id")` 존재 시 즉시 return
- `subtype in ("bot_message", "message_changed", "message_deleted")` 필터
- `_is_bot_message_by_heuristic()`: 봇 응답 접두사 패턴 매칭 (추가 방어선)

### 3.5 OpenRouter 무료 모델 fallback 체인

MODEL_SELECTION.md의 smoke test 결과를 반영한 fallback 체인이다.

```python
CLASSIFIER_FALLBACK_CHAIN = [
    "openai/gpt-oss-20b:free",      # 주 모델 (JSON PASS, 안정적)
    "google/gemma-4-31b-it:free",   # fallback (JSON 마크다운 래핑 파싱 필요)
]
QA_FALLBACK_CHAIN = [
    "nex-agi/nex-n2-pro:free",      # 주 모델 (한국어 최우선, 1.3s)
    "openai/gpt-oss-120b:free",     # fallback (느리지만 한국어 가능)
]
```

JSON 파싱은 마크다운 펜스(` ```json `)를 자동 제거하는 `_strip_json_fence()`가 처리한다.

### 3.6 pgvector Fallback (ENABLE_VECTOR_SEARCH=false)

`context_retriever.search_similar_embeddings()`가 단일 진입점이며, 내부에서 분기한다.

- `ENABLE_VECTOR_SEARCH=true` + pgvector 설치: 코사인 유사도 검색
- `ENABLE_VECTOR_SEARCH=false` 또는 pgvector 오류: 최근 메시지 N개 반환 (항상 빈 결과 없음)

### 3.7 한국어 토큰 추정

`len(text.split()) * 1.3` 방식은 한국어에서 실제 토큰 수를 **과소추정**한다 (공백 기준 단어 수 << 실제 BPE 서브워드 수). 예산 초과가 더 위험하므로 안전한 과추정 방식을 사용한다.

```python
korean_tokens = int(korean_chars / 1.5)   # 1.5자당 1토큰 (과추정)
english_tokens = int(english_words * 1.3)  # 1.3 단어당 1토큰
```

### 3.8 모델명 코드-프리 교체

`properties.txt` 파일 변경만으로 코드 수정 없이 모델을 교체할 수 있다 (PRD 리스크 완화 전략). `config.py`가 시작 시 해당 파일을 파싱하고 `CLASSIFIER_MODEL`, `QA_MODEL` 등을 설정한다.

---

## 4. 데이터베이스 스키마

### conversation_message

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| event_id | VARCHAR(100) UNIQUE | Slack event_ts 기반 중복 방지 |
| channel_id | VARCHAR(20) | Slack 채널 ID (인덱스) |
| thread_ts | VARCHAR(30) | 스레드 루트 ts (NULL 허용) |
| message_ts | VARCHAR(30) | 메시지 타임스탬프 |
| user_id | VARCHAR(20) | Slack 사용자 ID |
| role | VARCHAR(10) | 'user' 또는 'bot' |
| content | TEXT | PII 마스킹된 메시지 본문 |
| is_question | BOOLEAN | 분류기 판단 결과 |
| is_fallback | BOOLEAN | Fallback 발동 여부 |
| category | VARCHAR(20) | 레거시 — is_question으로 대체됨 |
| response_time_ms | INTEGER | 봇 응답 생성 소요 시간(ms) |
| prompt_tokens | INTEGER | LLM 요청 토큰 수 (추정) |
| completion_tokens | INTEGER | LLM 응답 토큰 수 (추정) |
| rag_avg_similarity | FLOAT | RAG 검색 결과 평균 코사인 유사도 |
| used_web_search | BOOLEAN | DuckDuckGo 보조 검색 사용 여부 |
| topic | VARCHAR(100) | LLM 추출 핵심 주제 태그 (자유 텍스트) |
| product_key | VARCHAR(50) | LLM 분류 제품 키 (예: "iruda_backend") |
| created_at | TIMESTAMP | 저장 시각 |

UNIQUE: `(channel_id, message_ts)` — 중복 삽입 방지 2차 방어선

### context_embedding

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| source_message_id | BIGINT FK | conversation_message.id (CASCADE DELETE) |
| chunk_text | TEXT | 임베딩 대상 텍스트 |
| chunk_type | VARCHAR(20) | 청크 유형 ('message' / 'thread') |
| embedding | vector(768) | pgvector 벡터 (ENABLE_VECTOR_SEARCH=true) |
| embedding_json | TEXT | JSON 직렬화 fallback |

인덱스: `USING ivfflat (embedding vector_cosine_ops) WITH (lists=100)`

### message_feedback

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| message_id | BIGINT FK | conversation_message.id (CASCADE DELETE) |
| user_id | VARCHAR(20) | 반응한 사용자 Slack ID |
| reaction | VARCHAR(50) | 이모지 이름 (thumbsup / thumbsdown 등) |
| sentiment | VARCHAR(10) | 'positive' / 'negative' |
| llm_failure_reason | VARCHAR(30) | LLM 분류 실패 원인 (wrong_source / hallucination / out_of_scope / format_issue) |
| user_failure_reason | VARCHAR(30) | 사용자가 직접 선택한 실패 원인 |
| created_at | TIMESTAMP | 반응 저장 시각 |

UNIQUE: `(message_id, user_id, reaction)` — 동일 반응 중복 저장 방지

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

## 5. 이벤트 처리 흐름

```
Slack 이벤트 수신
    │
    ├─ [즉시] ack() 응답 (< 3초 SLA 보장)
    ├─ [즉시] post_thinking_indicator() — "답변 생성 중..." 임시 메시지
    │
    └─ threading.Thread 분기 (LLM 작업)
         │
         ├─ 1. 사용자 메시지 PII 마스킹 후 DB 저장 + 임베딩 생성
         ├─ 2. classifier.py → 질문 여부 판별 (캐시 적용)
         │      └── NONE이면 종료 (저장은 유지)
         ├─ 3. context_retriever.py → RAG 벡터 검색 (top-5)
         ├─ 4. DB에서 최근 5개 메시지 조회
         ├─ 5. 토큰 예산(6000) 내로 프롬프트 구성
         ├─ 6. llm_service.call_qa() → 답변 생성 (nex-n2-pro)
         ├─ 7. llm_service.call_with_fallback() → Fallback 평가
         │      ├── can_answer=true: update_message() 로 임시 메시지 교체
         │      └── can_answer=false: send_fallback_message() 담당자 호출
         └─ 8. 봇 응답 DB 저장
```

---

## 6. 환경변수 설정

`.env.example`을 복사하여 `.env`로 저장 후 실제 값 입력.

| 변수 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `SLACK_BOT_TOKEN` | 필수 | - | xoxb- 형식 |
| `SLACK_APP_TOKEN` | 필수 | - | xapp- 형식 (Socket Mode) |
| `SLACK_SIGNING_SECRET` | 필수 | - | 이벤트 서명 검증 |
| `OPENROUTER_API_KEY` | 필수 | - | sk-or-v1- 형식 |
| `DATABASE_URL` | 필수 | - | postgresql://user:pass@host/db |
| `ENABLE_VECTOR_SEARCH` | 선택 | true | false면 pgvector 불필요 |
| `TARGET_CHANNEL_IDS` | 선택 | (없음) | 수집 채널 쉼표 구분 |
| `FALLBACK_MENTION_USER_IDS` | 선택 | (없음) | 기본 에스컬레이션 담당자 ID (DB product_categories 미등록 시 폴백) |
| `RUN_BACKFILL` | 선택 | false | true면 기동 시 백필 실행 |
| `LOG_LEVEL` | 선택 | INFO | DEBUG/INFO/WARNING/ERROR |

---

## 7. 배포 방법

### Docker Compose (권장)

```bash
# 1. 환경변수 설정
cp slack_bot/.env.example slack_bot/.env
# (vi slack_bot/.env 로 실제 토큰 입력)

# 2. 전체 스택 기동
docker compose up -d

# 3. 로그 확인
docker compose logs -f app

# 4. 최초 배포 시 백필 (선택)
docker compose exec app env RUN_BACKFILL=true python main.py
```

### 로컬 개발 실행

```bash
cd slack_bot
pip install -r requirements.txt
cp .env.example .env
# (.env 편집)
python main.py
```

---

## 8. 테스트

```bash
# 단위 테스트 (PII 필터, 토큰 카운터)
cd slack_bot
python -m pytest tests/ -v

# 문법 검증 (전체 파일)
python -m py_compile config.py
python -m py_compile main.py
# ... (각 파일)
```

통합 테스트(Slack API / OpenRouter / PostgreSQL)는 실제 자격증명이 필요하므로 이 구현의 범위 밖이다. 파일럿 채널에서 직접 검증을 권장한다.

---

## 9. 보안 체크리스트

- [x] 모든 시크릿은 `.env` 파일에만 존재 (`.gitignore`에 `.env` 포함)
- [x] `.env.example`에 실제 키 없음 (placeholder만 사용)
- [x] Slack 이벤트 서명 검증: Bolt 프레임워크 기본 기능 적용
- [x] PII 마스킹: 저장 전 `apply_pii_filter()` 필수 적용
- [x] DM 채널 수집 금지: `TARGET_CHANNEL_IDS` 설정으로 명시 채널만 수집
- [x] DB 포트: docker-compose에서 개발 시에만 노출 (주석 처리 권장)
- [x] 컨테이너 비루트 사용자 실행 (`botuser`, uid=1000)
- [x] 임베딩 모델 로컬 실행 (외부 API 의존성 없음)

---

## 10. 알려진 제약 및 추후 개선 사항

| 항목 | 현재 상태 | 개선 방향 |
|------|-----------|-----------|
| 임베딩 차원 | 768 (mpnet-base-v2) | PRD Q5 확정 시 `ko-sroberta-multitask` 검토 |
| ivfflat 인덱스 | lists=100 | 데이터 10만 건 이상 시 lists 조정 또는 hnsw 전환 |
| 분류기 캐시 | 인메모리 LRU 256개 | Redis 이관 시 다중 인스턴스 지원 |
| 백필 재시도 | 단순 break | 재개 가능한 cursor 저장 로직 추가 |
| 주제 태그 정규화 | Stage 2 구현 완료 (LLM canonical 통합) | 임베딩 클러스터링 기반 추가 정밀화 검토 가능 |
| RAG + 주제 통합 | 미적용 | 정규화된 canonical 주제를 메타데이터 필터 또는 boosting으로 활용 검토 |
| 히스토리 그룹핑 | 단순 시간순 목록 | canonical 주제별 그룹핑 및 동일 주제 과거 Q&A 요약 제시 |
| 모니터링 | 로그만 | Prometheus metrics 또는 Slack 알림 추가 검토 |
