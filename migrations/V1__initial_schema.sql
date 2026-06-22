-- V1: 초기 스키마 생성
-- 실행 환경: PostgreSQL 14+ with pgvector 확장
-- 주의: 이미 테이블이 존재하는 환경에서는 실행하지 않는다.

-- ────────────────────────────────────────────────────────────────────────────
-- 확장 설치
-- ────────────────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ────────────────────────────────────────────────────────────────────────────
-- conversation_message: Slack 채널 메시지 저장
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversation_message (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(100) UNIQUE,
    channel_id      VARCHAR(20)  NOT NULL,
    thread_ts       VARCHAR(30),
    message_ts      VARCHAR(30)  NOT NULL,
    user_id         VARCHAR(20),
    role            VARCHAR(10)  NOT NULL,   -- 'user' | 'bot'
    content         TEXT         NOT NULL,
    content_raw     TEXT,                    -- PII 마스킹 전 원문 (옵션)
    is_question     BOOLEAN,
    is_fallback     BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_channel_message_ts UNIQUE (channel_id, message_ts)
);

CREATE INDEX IF NOT EXISTS ix_conv_msg_event_id   ON conversation_message (event_id);
CREATE INDEX IF NOT EXISTS ix_conv_msg_channel_id ON conversation_message (channel_id);
CREATE INDEX IF NOT EXISTS ix_conv_msg_thread_ts  ON conversation_message (thread_ts);
CREATE INDEX IF NOT EXISTS ix_conv_msg_message_ts ON conversation_message (message_ts);

-- ────────────────────────────────────────────────────────────────────────────
-- context_embedding: 메시지 청크 벡터 임베딩
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_embedding (
    id                BIGSERIAL PRIMARY KEY,
    source_message_id BIGINT NOT NULL REFERENCES conversation_message(id) ON DELETE CASCADE,
    chunk_text        TEXT   NOT NULL,
    chunk_type        VARCHAR(20),           -- 'message' | 'thread'
    embedding_json    TEXT,                  -- pgvector 미사용 시 JSON 직렬화 fallback
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ctx_emb_source_message_id ON context_embedding (source_message_id);

-- ────────────────────────────────────────────────────────────────────────────
-- message_feedback: 봇 답변 이모지 피드백
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS message_feedback (
    id          BIGSERIAL PRIMARY KEY,
    channel_id  VARCHAR(20)  NOT NULL,
    message_ts  VARCHAR(30)  NOT NULL,      -- 봇 답변 ts
    user_id     VARCHAR(20)  NOT NULL,
    reaction    VARCHAR(50)  NOT NULL,      -- thumbsup, thumbsdown 등
    sentiment   VARCHAR(10)  NOT NULL,      -- 'positive' | 'negative'
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_feedback_ts_user_reaction UNIQUE (message_ts, user_id, reaction)
);

CREATE INDEX IF NOT EXISTS ix_msg_feedback_channel_id  ON message_feedback (channel_id);
CREATE INDEX IF NOT EXISTS ix_msg_feedback_message_ts  ON message_feedback (message_ts);

-- ────────────────────────────────────────────────────────────────────────────
-- context_summary: 채널별 기간 요약본
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_summary (
    id           BIGSERIAL PRIMARY KEY,
    channel_id   VARCHAR(20) NOT NULL,
    period_start DATE        NOT NULL,
    period_end   DATE        NOT NULL,
    summary_text TEXT        NOT NULL,
    created_at   TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ctx_summary_channel_id ON context_summary (channel_id);
