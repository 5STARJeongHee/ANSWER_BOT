# 환경변수 로드 및 전체 설정값 관리 모듈
from __future__ import annotations
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드 (UTF-8 우선, CP949 폴백 — Windows 한국어 환경 대응)
try:
    load_dotenv(encoding="utf-8")
except UnicodeDecodeError:
    load_dotenv(encoding="cp949")

logger = logging.getLogger(__name__)


def _load_properties(path: str) -> dict:
    """properties.txt 파일에서 모델 설정을 로드한다."""
    result = {}
    props_path = Path(path)
    if not props_path.exists():
        logger.warning(f"모델 설정 파일을 찾을 수 없음: {path}")
        return result
    with open(props_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
    return result


# 모델 설정 로드 (properties.txt)
_model_config_path = os.getenv("MODEL_CONFIG_PATH", "../properties.txt")
_props = _load_properties(_model_config_path)

# --- Slack 설정 ---
# os.getenv를 사용하여 임포트 시 즉시 KeyError가 발생하지 않도록 한다.
# 실제 존재 여부는 main.py에서 validate_config()를 호출하여 검증한다.
SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")
SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")

# 수집 대상 채널 ID 목록
_raw_channels = os.getenv("TARGET_CHANNEL_IDS", "")
TARGET_CHANNEL_IDS: list[str] = [c.strip() for c in _raw_channels.split(",") if c.strip()]

# Fallback 담당자 목록
_raw_fallback = os.getenv("FALLBACK_MENTION_USER_IDS", "")
FALLBACK_MENTION_USER_IDS: list[str] = [u.strip() for u in _raw_fallback.split(",") if u.strip()]

# --- OpenRouter API 설정 ---
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# --- 모델 설정 (properties.txt 우선, 환경변수 fallback) ---
CLASSIFIER_MODEL: str = _props.get("classifier_model", "openai/gpt-oss-20b:free")
QA_MODEL: str = _props.get("qa_model", "nex-agi/nex-n2-pro:free")
SUMMARY_MODEL: str = _props.get("summary_model", "nex-agi/nex-n2-pro:free")
RAG_QUERY_MODEL: str = _props.get("rag_query_model", "openai/gpt-oss-20b:free")
IMAGE_MODEL: str = _props.get("image", "nvidia/nemotron-nano-12b-v2-vl:free")

# Fallback 모델 체인 (주 모델 실패 시 순서대로 시도)
CLASSIFIER_FALLBACK_CHAIN: list[str] = [
    CLASSIFIER_MODEL,
    "google/gemma-4-31b-it:free",
]
QA_FALLBACK_CHAIN: list[str] = [
    QA_MODEL,
    "openai/gpt-oss-120b:free",
]
SUMMARY_FALLBACK_CHAIN: list[str] = [
    SUMMARY_MODEL,
    "openai/gpt-oss-120b:free",
]

# --- 데이터베이스 설정 ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# --- 벡터 검색 설정 ---
ENABLE_VECTOR_SEARCH: bool = os.getenv("ENABLE_VECTOR_SEARCH", "true").lower() == "true"
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
EMBEDDING_DIM: int = int(os.getenv("EMBEDDING_DIM", "768"))

# RAG 검색 상위 K개
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "5"))
# 최근 대화 포함 메시지 수
RECENT_MESSAGE_COUNT: int = int(os.getenv("RECENT_MESSAGE_COUNT", "5"))

# --- LLM 설정 ---
MAX_CONTEXT_TOKENS: int = int(os.getenv("MAX_CONTEXT_TOKENS", "6000"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
CLASSIFIER_MAX_TOKENS: int = 80
QA_MAX_TOKENS: int = 500
SUMMARY_MAX_TOKENS: int = 300

# --- 배치 스케줄 설정 ---
SUMMARY_BATCH_HOUR: int = int(os.getenv("SUMMARY_BATCH_HOUR", "2"))
SUMMARY_BATCH_WEEKDAY: int = int(os.getenv("SUMMARY_BATCH_WEEKDAY", "0"))

# --- 로깅 설정 ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"


def validate_config() -> None:
    """필수 환경변수가 설정됐는지 검증하고 경고를 출력한다."""
    required = {
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
        "SLACK_APP_TOKEN": SLACK_APP_TOKEN,
        "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
        "DATABASE_URL": DATABASE_URL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"필수 환경변수 누락: {', '.join(missing)}")

    if not TARGET_CHANNEL_IDS:
        logger.warning("TARGET_CHANNEL_IDS가 비어 있음. app_mention 이벤트만 처리됩니다.")

    if not ENABLE_VECTOR_SEARCH:
        logger.warning("ENABLE_VECTOR_SEARCH=false. pgvector 없이 텍스트 기반 검색으로 동작합니다.")

    logger.info(
        f"설정 로드 완료 | classifier={CLASSIFIER_MODEL} | qa={QA_MODEL} "
        f"| vector_search={ENABLE_VECTOR_SEARCH} | embedding_dim={EMBEDDING_DIM}"
    )
