-- Slack 챗봇 초기 스키마 마이그레이션 SQL (Alembic 대신 단독 실행 가능)
-- 실행 순서: 이 파일을 psql로 직접 실행하거나 Alembic env.py에서 참조한다

-- pgvector 확장 활성화 (pgvector Docker 이미지 필요)
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- conversation_message: Slack 메시지 원문 저장 테이블
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_message (
    id          BIGSERIAL PRIMARY KEY,
    event_id    VARCHAR(100) UNIQUE,
    channel_id  VARCHAR(20) NOT NULL,
    thread_ts   VARCHAR(30),
    message_ts  VARCHAR(30) NOT NULL,
    user_id     VARCHAR(20),
    role        VARCHAR(10) NOT NULL,      -- 'user' | 'bot'
    content     TEXT NOT NULL,
    content_raw TEXT,                      -- PII 마스킹 전 원문 (옵션)
    is_question BOOLEAN,
    is_fallback BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_channel_message_ts UNIQUE (channel_id, message_ts)
);

CREATE INDEX IF NOT EXISTS ix_conversation_message_channel_id
    ON conversation_message (channel_id);

CREATE INDEX IF NOT EXISTS ix_conversation_message_thread_ts
    ON conversation_message (thread_ts)
    WHERE thread_ts IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_conversation_message_message_ts
    ON conversation_message (message_ts);

CREATE INDEX IF NOT EXISTS ix_conversation_message_event_id
    ON conversation_message (event_id)
    WHERE event_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- context_embedding: 메시지 청크 벡터 임베딩 테이블
-- 임베딩 차원: 768 (paraphrase-multilingual-mpnet-base-v2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_embedding (
    id                 BIGSERIAL PRIMARY KEY,
    source_message_id  BIGINT NOT NULL REFERENCES conversation_message(id) ON DELETE CASCADE,
    chunk_text         TEXT NOT NULL,
    embedding          vector(768),        -- pgvector: 코사인 유사도 검색용
    embedding_json     TEXT,               -- fallback: JSON 직렬화 벡터
    created_at         TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_context_embedding_source_message_id
    ON context_embedding (source_message_id);

-- ivfflat 인덱스: 코사인 유사도 검색 성능 최적화
-- 주의: 데이터가 최소 1000건 이상일 때 효과적
CREATE INDEX IF NOT EXISTS ix_context_embedding_vector
    ON context_embedding USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- context_summary: 채널별 주기적 대화 요약본 테이블 (V2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_summary (
    id            BIGSERIAL PRIMARY KEY,
    channel_id    VARCHAR(20) NOT NULL,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    summary_text  TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_context_summary_channel_id
    ON context_summary (channel_id);

CREATE INDEX IF NOT EXISTS ix_context_summary_period_end
    ON context_summary (period_end DESC);
