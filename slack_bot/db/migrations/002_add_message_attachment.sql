-- message_attachment 테이블 추가 — conversation_message와 1:N 관계로 첨부파일 분석 결과 저장
CREATE TABLE IF NOT EXISTS message_attachment (
    id            BIGSERIAL PRIMARY KEY,
    message_id    BIGINT NOT NULL REFERENCES conversation_message(id) ON DELETE CASCADE,
    slack_file_id VARCHAR(50),
    file_name     VARCHAR(255),
    mime_type     VARCHAR(100),
    file_type     VARCHAR(20) NOT NULL,
    analysis_text TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_message_attachment_message_id ON message_attachment(message_id);
