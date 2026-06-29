# Prometheus 메트릭 수집 유틸리티
from prometheus_client import Counter, Histogram

# 메시지 처리 건수
MESSAGE_PROCESSED_TOTAL = Counter(
    "slackbot_message_processed_total",
    "Total number of messages processed by the bot",
    ["status"] # 'success', 'error', 'ignored'
)

# LLM API 호출 소요 시간
LLM_REQUEST_DURATION_SECONDS = Histogram(
    "slackbot_llm_request_duration_seconds",
    "Time spent calling LLM API",
    ["model", "endpoint_type"] # endpoint_type: 'classifier', 'qa', 'summary', 'vision'
)

# RAG 검색 건수
RAG_SEARCH_TOTAL = Counter(
    "slackbot_rag_search_total",
    "Total number of RAG searches performed"
)
