-- V3: RAG 품질·웹검색 의존 추적 컬럼 추가
-- 대상 테이블: conversation_message
-- IF NOT EXISTS 사용 — 중복 실행 안전

-- RAG 컨텍스트 top-k 평균 유사도 (0.0 ~ 1.0). bot role 행에만 기록.
-- 낮을수록 과거 대화에 관련 정보가 부족했음을 의미.
ALTER TABLE conversation_message
    ADD COLUMN IF NOT EXISTS rag_avg_similarity FLOAT;

-- 웹 검색 보조 사용 여부. bot role 행에만 기록.
-- TRUE이면 RAG 유사도 부족으로 DuckDuckGo 검색 결과를 프롬프트에 포함했음.
ALTER TABLE conversation_message
    ADD COLUMN IF NOT EXISTS used_web_search BOOLEAN DEFAULT FALSE;

-- RAG 유사도 범위 조회 성능 인덱스
CREATE INDEX IF NOT EXISTS ix_conv_msg_rag_similarity
    ON conversation_message (rag_avg_similarity)
    WHERE rag_avg_similarity IS NOT NULL;
