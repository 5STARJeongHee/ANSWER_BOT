-- V2: 히스토리·대시보드 기능을 위한 분석 컬럼 추가
-- 대상 테이블: conversation_message
-- IF NOT EXISTS 사용 — 중복 실행 안전

-- 메시지 분류 카테고리 (QUESTION / REQUEST / NONE)
ALTER TABLE conversation_message
    ADD COLUMN IF NOT EXISTS category VARCHAR(20);

-- 봇 응답 생성 소요 시간 (ms). bot role 행에만 기록.
ALTER TABLE conversation_message
    ADD COLUMN IF NOT EXISTS response_time_ms INTEGER;

-- LLM에 전달한 프롬프트 추정 토큰 수 (입력).
-- user 행: 질문 메시지 토큰 수.
-- bot 행: RAG 컨텍스트 + 질문 등 LLM 호출 시 전달한 전체 메시지 토큰 수.
ALTER TABLE conversation_message
    ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER;

-- LLM이 생성한 답변 추정 토큰 수 (출력). bot role 행에만 기록.
ALTER TABLE conversation_message
    ADD COLUMN IF NOT EXISTS completion_tokens INTEGER;

-- 카테고리별 조회 성능을 위한 인덱스
CREATE INDEX IF NOT EXISTS ix_conv_msg_category
    ON conversation_message (category)
    WHERE category IS NOT NULL;

-- 대시보드 기간 집계 성능을 위한 created_at 복합 인덱스
CREATE INDEX IF NOT EXISTS ix_conv_msg_created_at_role
    ON conversation_message (created_at, role);

-- ────────────────────────────────────────────────────────────────────────────
-- pgvector 임베딩 컬럼 (ENABLE_VECTOR_SEARCH=true 환경에서만 실행)
-- 아래 두 구문은 pgvector 확장이 활성화된 경우에만 실행한다.
-- EMBEDDING_DIM은 사용 모델에 맞게 교체한다 (기본 768).
-- ────────────────────────────────────────────────────────────────────────────
-- ALTER TABLE context_embedding ADD COLUMN IF NOT EXISTS embedding vector(768);
-- CREATE INDEX IF NOT EXISTS ix_context_embedding_vector
--     ON context_embedding USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 100);
