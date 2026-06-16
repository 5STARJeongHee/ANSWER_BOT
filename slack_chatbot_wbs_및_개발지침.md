# 사내 Slack Q&A 챗봇 개발 - WBS 및 개발 지침

## 1. 프로젝트 개요

**목적**: 사내 무료 버전 Slack에서 직원들이 올리는 질문/요청에 자동으로 답변하고, 최근 및 과거 대화 내역을 지속적으로 수집·요약하여 컨텍스트로 활용하는 챗봇 구축.

**핵심 요구사항**
- Slack 채널/스레드에 올라오는 질문·요청 감지
- LLM 기반 자동 응답 생성
- 최근 대화 + 과거 대화 이력을 컨텍스트 저장소(DB/Vector Store)에 누적 저장
- 누적된 컨텍스트를 검색(RAG)하여 답변 품질 향상
- Slack 무료 플랜 제약(메시지 보관 90일, 앱 권한 제한 등) 고려

**기술 스택 (Python 기준)**
- Python 3.11+
- Slack Bolt for Python (Socket Mode 권장 - 무료 플랜에서 별도 도메인/HTTPS 없이 동작)
- DB: PostgreSQL (대화 로그/메타데이터) + pgvector (벡터 검색), SQLAlchemy ORM
- LLM: Anthropic Claude API (`anthropic` SDK) 또는 OpenAI
- 임베딩: `sentence-transformers` 또는 OpenAI Embeddings API
- RAG: LangChain 또는 직접 구현 (단순 구조라면 직접 구현 추천)
- 배치/스케줄: APScheduler (인프라 추가 없이 프로세스 내 스케줄링)
- 배포: Docker + docker-compose (사내 서버 또는 클라우드 VM)

---

## 2. 시스템 아키텍처 개요

```
[Slack Workspace]
   │ (Socket Mode / Events API)
   ▼
[Python App (Bolt for Python)]
   ├── event_handler.py      (메시지 이벤트 수신 - @app.event)
   ├── classifier.py         (질문/요청 여부 판별)
   ├── context_retriever.py  (pgvector 검색 - RAG)
   ├── llm_service.py        (Claude API 호출)
   ├── collector.py          (대화 수집 배치 - APScheduler)
   ├── summarizer.py         (장기 요약 생성)
   └── slack_service.py      (응답 전송)
        │
        ▼
[PostgreSQL + pgvector] ── 대화 원문, 임베딩, 요약본
```

---

## 3. WBS (단계별 작업 분해)

### Phase 1. 요구사항 정의 및 설계 (1주)
1.1. 대상 채널/스레드 범위 정의 (전사 공통 채널 vs 특정 팀 채널)
1.2. "질문/요청"으로 분류할 트리거 조건 정의 (멘션, 키워드, 질문형 문장 등)
1.3. 응답 정책 정의 (즉시 답변 vs 담당자 호출, 답변 불가 시 처리 방안)
1.4. 데이터 보관 정책 정의 (수집 범위, 보존 기간, 민감정보 마스킹)
1.5. 아키텍처/ERD 설계 문서화

### Phase 2. Slack 연동 기본 구축 (1~2주)
2.1. Slack App 생성 및 Bot Token, Socket Mode 설정
2.2. 필요 OAuth Scope 설정 (`channels:history`, `chat:write`, `users:read`, `app_mentions:read` 등)
2.3. Python 프로젝트 셋업 (가상환경, `slack-bolt`, `anthropic`, `sqlalchemy`, `pgvector` 등 의존성 설치)
2.4. 이벤트 수신 핸들러 구현 (message, app_mention)
2.5. 단순 echo 응답으로 연동 테스트

### Phase 3. 대화 수집 및 저장소 구축 (2주)
3.1. PostgreSQL 스키마 설계 (channel, thread, user, message, timestamp, role 등)
3.2. 실시간 메시지 적재 로직 구현 (이벤트 수신 시 저장)
3.3. 과거 대화 백필(backfill) 배치 구현 (`conversations.history` API, 90일 제한 고려)
3.4. 개인정보/민감정보 필터링 로직 적용
3.5. 대화 데이터 정합성 검증 (중복/누락 체크)

### Phase 4. 질문 판별 및 응답 생성 (2주)
4.1. 메시지 분류 로직 구현 (질문/요청 여부 - 규칙 기반 + LLM 보조)
4.2. Claude API 연동 (Spring 설정, API Key 관리 - 환경변수/Secret)
4.3. 프롬프트 템플릿 설계 (역할, 톤, 출력 형식)
4.4. 응답 생성 → Slack 스레드 회신 로직 구현
4.5. 응답 불확실 시 "담당자 호출" fallback 로직

### Phase 5. 컨텍스트 관리 (RAG) 구축 (2~3주)
5.1. 임베딩 모델 선정 및 연동
5.2. 대화 내용 청킹(chunking) 및 임베딩 적재 파이프라인
5.3. Vector Store 구축 (pgvector 등) 및 검색 API 구현
5.4. 질문 발생 시 관련 과거 대화 검색 → 프롬프트에 컨텍스트 주입
5.5. 주기적 대화 요약(summary) 생성 배치 (장기 컨텍스트 압축)
5.6. 컨텍스트 신선도 관리 (오래된/무효 정보 정리 정책)

### Phase 6. 테스트 및 품질 검증 (1~2주)
6.1. 단위 테스트 (분류기, 프롬프트 빌더, API 클라이언트)
6.2. 통합 테스트 (Slack 이벤트 → 응답까지 E2E)
6.3. 답변 품질 검증용 테스트셋 작성 및 평가
6.4. 부하/장애 시나리오 테스트 (Slack rate limit, LLM 오류 등)

### Phase 7. 배포 및 운영 (1주~)
7.1. Docker 이미지화 및 배포 환경 구성
7.2. 로깅/모니터링 구축 (응답 성공률, 응답 시간, 오류 알림)
7.3. 운영 매뉴얼 작성 (프롬프트 수정 방법, 채널 추가 방법 등)
7.4. 점진적 오픈 (파일럿 채널 → 전사 확대)
7.5. 사용자 피드백 수집 및 개선 루프 운영

---

## 4. 개발 지침 (Python 기준)

### 4.1 프로젝트 구조 예시
```
slack_bot/
 ├── .env                    (SLACK_BOT_TOKEN, ANTHROPIC_API_KEY 등 - git 제외)
 ├── requirements.txt
 ├── main.py                 (앱 진입점 - Bolt 앱 시작, APScheduler 등록)
 ├── config.py               (환경변수 로드, 설정값 관리)
 ├── db/
 │    ├── models.py          (SQLAlchemy 모델 - Message, Embedding, Summary)
 │    ├── repository.py      (CRUD 함수)
 │    └── migrations/        (Alembic 마이그레이션)
 ├── handlers/
 │    └── event_handler.py   (Bolt @app.event, @app.mention 핸들러)
 ├── services/
 │    ├── classifier.py      (질문/요청 분류)
 │    ├── llm_service.py     (Claude API 호출, 프롬프트 빌드)
 │    ├── context_retriever.py (pgvector 유사도 검색)
 │    ├── summarizer.py      (대화 요약 배치)
 │    └── slack_service.py   (메시지 전송, 이력 조회)
 ├── batch/
 │    ├── collector.py       (과거 대화 백필)
 │    └── scheduler.py       (APScheduler 작업 등록)
 └── utils/
      ├── pii_filter.py      (민감정보 마스킹)
      └── token_counter.py   (LLM 토큰 수 추정)
```

### 4.2 핵심 원칙
- **무료 Slack 플랜 제약 인지**: 메시지 이력 조회는 최근 90일로 제한될 수 있음 → 실시간 수집을 최우선으로 하고, 백필은 보조 수단으로 설계.
- **API Key/Token 관리**: `python-dotenv`로 `.env` 로드, 코드/리포지토리에 노출 금지.
- **비동기 처리**: Slack 이벤트 응답은 3초 내 ack 필요 → `say()` 즉시 호출 후 LLM/DB 작업은 `threading.Thread` 또는 `asyncio`로 분리.
- **멱등성**: Slack 이벤트 재전송 가능성 대비, `event_id` 기준 DB unique 체크로 중복 처리 방지.
- **컨텍스트 길이 관리**: LLM 컨텍스트 윈도우 초과 방지를 위해 RAG 검색 결과 + 최근 N개 메시지로 제한.
- **민감정보 처리**: 저장 전 PII(이메일, 전화번호 등) 마스킹 또는 별도 컬럼 분리.
- **점진적 릴리스**: 분류기/프롬프트 변경 시 A/B 또는 파일럿 채널에서 우선 검증.

### 4.3 데이터베이스 스키마 예시
```sql
CREATE TABLE conversation_message (
    id BIGSERIAL PRIMARY KEY,
    channel_id VARCHAR(20),
    thread_ts VARCHAR(30),
    message_ts VARCHAR(30),
    user_id VARCHAR(20),
    role VARCHAR(10),       -- 'user' / 'bot'
    content TEXT,
    is_question BOOLEAN,
    created_at TIMESTAMP
);

CREATE TABLE context_embedding (
    id BIGSERIAL PRIMARY KEY,
    source_message_id BIGINT REFERENCES conversation_message(id),
    chunk_text TEXT,
    embedding VECTOR(1536),
    created_at TIMESTAMP
);

CREATE TABLE context_summary (
    id BIGSERIAL PRIMARY KEY,
    channel_id VARCHAR(20),
    period_start DATE,
    period_end DATE,
    summary_text TEXT,
    created_at TIMESTAMP
);
```

---

## 5. 단계별 필요 프롬프트 (LLM 프롬프트 템플릿)

### 5.1 질문/요청 판별 프롬프트 (Phase 4.1)
```
역할: 너는 사내 Slack 메시지를 분류하는 분류기다.
입력된 메시지가 "질문" 또는 "업무 요청"에 해당하는지 판단하라.

분류 기준:
- 질문: 정보, 방법, 상태 등을 묻는 문장
- 요청: 특정 작업/처리를 부탁하는 문장
- 해당 없음: 잡담, 공지, 감사 인사 등

출력 형식 (JSON):
{"category": "QUESTION | REQUEST | NONE", "confidence": 0.0~1.0, "reason": "간단한 이유"}

메시지: "{message_text}"
```

### 5.2 답변 생성 프롬프트 (Phase 4.3)
```
역할: 너는 사내 업무 지원 Slack 챗봇이다. 정확하고 간결하게 답변하라.
모르는 내용은 추측하지 말고 "확인이 필요합니다"라고 답하라.

[참고 컨텍스트 - 과거 관련 대화 요약]
{retrieved_context}

[최근 대화 이력]
{recent_messages}

[현재 질문]
작성자: {user_name}
내용: {message_text}

답변 작성 시 유의사항:
- 한국어로 작성
- 2~5문장 내로 핵심만 전달
- 추가 확인이 필요하면 누구에게 문의해야 하는지 안내
```

### 5.3 RAG 검색 쿼리 생성 프롬프트 (Phase 5.4)
```
다음 질문에서 핵심 키워드와 의도를 추출하여,
과거 대화 검색에 사용할 검색 쿼리 문장을 1~2개 생성하라.

질문: "{message_text}"

출력: 검색 쿼리 목록 (줄바꿈 구분)
```

### 5.4 대화 요약 프롬프트 (Phase 5.5, 배치용)
```
역할: 너는 사내 Slack 채널의 대화를 요약하는 어시스턴트다.

아래는 {channel_name} 채널의 {period_start}~{period_end} 기간 대화 로그다.
이후 챗봇이 참고할 수 있도록 다음 항목으로 요약하라:

1. 주요 논의/이슈 (3~5개, 핵심만)
2. 결정된 사항 또는 합의 내용
3. 자주 반복되는 질문/주제
4. 추후 참고할 용어/약어 정리

[대화 로그]
{conversation_log}
```

### 5.5 Fallback(담당자 호출) 판단 프롬프트 (Phase 4.5)
```
아래 답변 초안이 사용자의 질문에 대해 충분히 신뢰할 수 있는 답변인지 평가하라.

질문: "{message_text}"
답변 초안: "{draft_answer}"

평가 기준:
- 사실 확인이 필요한 정책/수치/일정 정보를 포함하는가
- 컨텍스트에 근거 없이 추측한 부분이 있는가

출력 (JSON):
{"can_answer_directly": true/false, "fallback_message": "담당자 호출용 안내 문구 (필요 시)"}
```

---

## 6. 다음 단계 제안
- Phase 1 요구사항 정의 워크숍 진행 (채널 범위, 응답 정책 확정)
- Slack App 생성 권한 확인 (Workspace Admin 필요)
- Claude API 사용 정책/예산 확인
