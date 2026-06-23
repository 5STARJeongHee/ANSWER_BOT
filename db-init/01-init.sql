-- QNA BOT 초기 스키마 (신규 DB 생성 시 1회 실행)
-- Docker: ./db-init → /docker-entrypoint-initdb.d/01-init.sql

-- ────────────────────────────────────────────────────────────────────────────
-- 확장
-- ────────────────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ────────────────────────────────────────────────────────────────────────────
-- conversation_message
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversation_message (
    id                  BIGSERIAL    PRIMARY KEY,
    event_id            VARCHAR(100) UNIQUE,
    channel_id          VARCHAR(20)  NOT NULL,
    thread_ts           VARCHAR(30),
    message_ts          VARCHAR(30)  NOT NULL,
    user_id             VARCHAR(20),
    role                VARCHAR(10)  NOT NULL,       -- 'user' | 'bot'
    content             TEXT         NOT NULL,
    content_raw         TEXT,                        -- PII 마스킹 전 원문 (옵션)
    is_question         BOOLEAN,
    is_fallback         BOOLEAN      DEFAULT FALSE,

    response_time_ms    INTEGER,                     -- 봇 응답 생성 소요 시간 (ms)
    prompt_tokens       INTEGER,                     -- LLM 입력 추정 토큰
    completion_tokens   INTEGER,                     -- LLM 출력 추정 토큰
    rag_avg_similarity  FLOAT,                       -- RAG top-k 평균 유사도 (0~1)
    used_web_search     BOOLEAN      DEFAULT FALSE,  -- 웹 검색 보조 사용 여부
    topic               VARCHAR(200),                -- LLM 추출 핵심 주제 태그 (예: "Redis 연결 오류")
    created_at          TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_channel_message_ts UNIQUE (channel_id, message_ts)
);

CREATE INDEX IF NOT EXISTS ix_conv_msg_event_id        ON conversation_message (event_id);
CREATE INDEX IF NOT EXISTS ix_conv_msg_channel_id      ON conversation_message (channel_id);
CREATE INDEX IF NOT EXISTS ix_conv_msg_thread_ts       ON conversation_message (thread_ts);
CREATE INDEX IF NOT EXISTS ix_conv_msg_message_ts      ON conversation_message (message_ts);
CREATE INDEX IF NOT EXISTS ix_conv_msg_created_at_role ON conversation_message (created_at, role);
CREATE INDEX IF NOT EXISTS ix_conv_msg_category
    ON conversation_message (category)
    WHERE category IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_conv_msg_rag_similarity
    ON conversation_message (rag_avg_similarity)
    WHERE rag_avg_similarity IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_conv_msg_topic
    ON conversation_message (topic)
    WHERE topic IS NOT NULL;

-- ────────────────────────────────────────────────────────────────────────────
-- context_embedding
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_embedding (
    id                BIGSERIAL PRIMARY KEY,
    source_message_id BIGINT    NOT NULL REFERENCES conversation_message(id) ON DELETE CASCADE,
    chunk_text        TEXT      NOT NULL,
    chunk_type        VARCHAR(20),           -- 'message' | 'thread'
    embedding         vector(768),           -- pgvector 컬럼 (EMBEDDING_DIM=768 기준)
    embedding_json    TEXT,                  -- pgvector 미사용 환경 fallback
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ctx_emb_source_message_id ON context_embedding (source_message_id);
CREATE INDEX IF NOT EXISTS ix_ctx_emb_vector
    ON context_embedding USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX IF NOT EXISTS ix_ctx_emb_trgm
    ON context_embedding USING gin (chunk_text gin_trgm_ops);

-- ────────────────────────────────────────────────────────────────────────────
-- message_feedback
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS message_feedback (
    id          BIGSERIAL    PRIMARY KEY,
    channel_id  VARCHAR(20)  NOT NULL,
    message_ts  VARCHAR(30)  NOT NULL,       -- 봇 답변 ts
    user_id     VARCHAR(20)  NOT NULL,
    reaction    VARCHAR(50)  NOT NULL,       -- thumbsup | thumbsdown 등
    sentiment   VARCHAR(10)  NOT NULL,       -- 'positive' | 'negative'
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_feedback_ts_user_reaction UNIQUE (message_ts, user_id, reaction)
);

CREATE INDEX IF NOT EXISTS ix_msg_feedback_channel_id ON message_feedback (channel_id);
CREATE INDEX IF NOT EXISTS ix_msg_feedback_message_ts ON message_feedback (message_ts);

-- ────────────────────────────────────────────────────────────────────────────
-- context_summary
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_summary (
    id           BIGSERIAL   PRIMARY KEY,
    channel_id   VARCHAR(20) NOT NULL,
    period_start DATE        NOT NULL,
    period_end   DATE        NOT NULL,
    summary_text TEXT        NOT NULL,
    created_at   TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ctx_summary_channel_id ON context_summary (channel_id);

-- ────────────────────────────────────────────────────────────────────────────
-- bot_settings
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bot_settings (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT         NOT NULL,
    updated_at TIMESTAMP    NOT NULL DEFAULT NOW()
);
