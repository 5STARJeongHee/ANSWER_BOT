# 사내 Slack Q&A 챗봇 백엔드 구현 설명서

> 작성일: 2026-06-16  
> 구현 언어: Python 3.8+  
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
   │     └── @app.event("message")     → ack() 즉시 + threading.Thread 분리
   │
   ├── [services/classifier.py]        → gpt-oss-20b 분류 (질문/요청/NONE)
   ├── [services/context_retriever.py] → pgvector 유사도 검색 (RAG)
   ├── [services/llm_service.py]       → OpenRouter API 호출 + 재시도/fallback
   ├── [services/slack_service.py]     → 메시지 전송, 이력 조회
   ├── [services/summarizer.py]        → 대화 요약 배치 (V2)
   │
   ├── [batch/collector.py]            → 90일 과거 대화 백필
   ├── [batch/scheduler.py]            → APScheduler 주간 요약 등록
   │
   ├── [db/models.py]                  → SQLAlchemy ORM + scoped_session
   ├── [db/repository.py]             → CRUD 함수 (모든 DB 접근 중앙화)
   │
   └── [utils/]
         ├── pii_filter.py            → 이메일/전화/주민번호 마스킹
         └── token_counter.py         → 한국어 혼합 토큰 추정

[PostgreSQL 15 + pgvector]
   ├── conversation_message           (메시지 원문, event_id UNIQUE)
   ├── context_embedding              (벡터 768차원, ivfflat 인덱스)
   └── context_summary                (주간 요약본, V2)
```

---

## 2. 파일별 역할

| 파일 | 역할 | 핵심 결정사항 |
|------|------|--------------|
| `main.py` | 앱 진입점, 초기화 오케스트레이션 | DB → 핸들러 → 스케줄러 → Socket Mode 순 기동 |
| `config.py` | 환경변수 + properties.txt 로드 | 모델명은 properties.txt 우선, env fallback |
| `db/models.py` | ORM 모델, 엔진/세션 팩토리 | scoped_session으로 스레드 안전 보장 |
| `db/repository.py` | CRUD 함수 중앙화 | 모든 DB 접근은 이 모듈만 사용 |
| `handlers/event_handler.py` | Bolt 이벤트 처리 | ack() 즉시 → Thread 분리 패턴 |
| `services/classifier.py` | 메시지 분류 (QUESTION/REQUEST/NONE) | 인메모리 LRU 캐시 256개 |
| `services/llm_service.py` | OpenRouter API 통합 | 지수 백오프 + 모델 fallback 체인 |
| `services/context_retriever.py` | RAG 벡터 검색 | pgvector 없으면 텍스트 fallback |
| `services/slack_service.py` | Slack 메시지 전송/조회 | rate limit 재시도, Retry-After 헤더 준수 |
| `services/summarizer.py` | 주간 대화 요약 | V2 배치, APScheduler 월요일 02시 실행 |
| `batch/collector.py` | 90일 백필 | 1.3초 간격 rate limit 대응 |
| `batch/scheduler.py` | APScheduler 등록 | coalesce=True, max_instances=1 |
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
| created_at | TIMESTAMP | 저장 시각 |

UNIQUE: `(channel_id, message_ts)` — 중복 삽입 방지 2차 방어선

### context_embedding

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BIGSERIAL PK | |
| source_message_id | BIGINT FK | conversation_message.id (CASCADE DELETE) |
| chunk_text | TEXT | 임베딩 대상 텍스트 |
| embedding | vector(768) | pgvector 벡터 (ENABLE_VECTOR_SEARCH=true) |
| embedding_json | TEXT | JSON 직렬화 fallback |

인덱스: `USING ivfflat (embedding vector_cosine_ops) WITH (lists=100)`

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
| `FALLBACK_MENTION_USER_IDS` | 선택 | (없음) | 담당자 Slack User ID |
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
| 피드백 수집 | 미구현 | V2에서 reaction_added 이벤트로 thumbs up/down 수집 |
| 모니터링 | 로그만 | Prometheus metrics 또는 Slack 알림 추가 검토 |
