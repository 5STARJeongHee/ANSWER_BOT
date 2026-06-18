# 환경변수 로드 및 전체 설정값 관리 모듈
from __future__ import annotations
import os
import logging
from pathlib import Path
import yaml
from dotenv import load_dotenv

# .env 파일 로드 (UTF-8 우선, CP949 폴백 — Windows 한국어 환경 대응)
try:
    load_dotenv(encoding="utf-8")
except UnicodeDecodeError:
    load_dotenv(encoding="cp949")

logger = logging.getLogger(__name__)


def _load_properties(path: str) -> dict:
    """properties.yml 파일에서 모델 설정을 로드한다."""
    props_path = Path(path)
    if not props_path.exists():
        logger.warning(f"모델 설정 파일을 찾을 수 없음: {path}")
        return {}
    with open(props_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        logger.warning(f"모델 설정 파일 형식 오류 (dict 아님): {path}")
        return {}
    # 모든 값을 문자열로 정규화한다.
    return {k: str(v) for k, v in data.items()}


# 모델 설정 로드 (properties.yml)
_model_config_path = os.getenv("MODEL_CONFIG_PATH", "../properties.yml")
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

# --- LLM 백엔드 선택 ---
# "openrouter": OpenRouter API (기본, API 키 필요)
# "ollama":     로컬 Ollama 서버 (API 키 불필요, 서버 자체 운용)
LLM_BACKEND: str = os.getenv("LLM_BACKEND", "openrouter").lower()

# --- OpenRouter API 설정 ---
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# --- Ollama 설정 (LLM_BACKEND=ollama 일 때 사용) ---
# Docker 컨테이너 내부: http://ollama:11434/v1 / 직접 실행: http://localhost:11434/v1
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_CLASSIFIER_MODEL: str = _props.get("ollama_classifier_model", "exaone3.5:2.4b-it-q4_K_M")
OLLAMA_QA_MODEL: str = _props.get("ollama_qa_model", "qwen2.5:32b-instruct-q4_K_M")
OLLAMA_SUMMARY_MODEL: str = _props.get("ollama_summary_model", "qwen2.5:32b-instruct-q4_K_M")
OLLAMA_RAG_QUERY_MODEL: str = _props.get("ollama_rag_query_model", "exaone3.5:2.4b-it-q4_K_M")
OLLAMA_IMAGE_MODEL: str = _props.get("ollama_image", "qwen2-vl:7b-instruct-q4_K_M")

# --- 통합 클라이언트 파라미터 (백엔드에 따라 결정) ---
_is_ollama: bool = LLM_BACKEND == "ollama"
LLM_API_KEY: str = "ollama" if _is_ollama else OPENROUTER_API_KEY
LLM_BASE_URL: str = OLLAMA_BASE_URL if _is_ollama else OPENROUTER_BASE_URL
# Ollama CPU 추론은 30초 이상 소요될 수 있으므로 타임아웃을 넉넉히 설정한다.
LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "120.0" if _is_ollama else "30.0"))

# --- 모델 설정 (properties.txt 우선, 환경변수 fallback) ---
CLASSIFIER_MODEL: str = _props.get("classifier_model", "openai/gpt-oss-20b:free")
QA_MODEL: str = _props.get("qa_model", "nex-agi/nex-n2-pro:free")
SUMMARY_MODEL: str = _props.get("summary_model", "nex-agi/nex-n2-pro:free")
RAG_QUERY_MODEL: str = _props.get("rag_query_model", "openai/gpt-oss-20b:free")
IMAGE_MODEL: str = _props.get("image", "nvidia/nemotron-nano-12b-v2-vl:free")

# --- Fallback 모델 체인 ---
# Ollama: rate limit이 없으므로 단일 모델만 유지
# OpenRouter: 주 모델 실패 시 순서대로 시도
if _is_ollama:
    CLASSIFIER_FALLBACK_CHAIN: list[str] = [OLLAMA_CLASSIFIER_MODEL]
    QA_FALLBACK_CHAIN: list[str] = [OLLAMA_QA_MODEL]
    SUMMARY_FALLBACK_CHAIN: list[str] = [OLLAMA_SUMMARY_MODEL]
    RAG_QUERY_FALLBACK_CHAIN: list[str] = [OLLAMA_RAG_QUERY_MODEL]
    IMAGE_MODEL = OLLAMA_IMAGE_MODEL
else:
    CLASSIFIER_FALLBACK_CHAIN = [CLASSIFIER_MODEL, "google/gemma-4-31b-it:free"]
    QA_FALLBACK_CHAIN = [QA_MODEL, "openai/gpt-oss-120b:free"]
    SUMMARY_FALLBACK_CHAIN = [SUMMARY_MODEL, "openai/gpt-oss-120b:free"]
    RAG_QUERY_FALLBACK_CHAIN = [RAG_QUERY_MODEL, CLASSIFIER_MODEL]

# --- 데이터베이스 설정 ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# --- 벡터 검색 설정 ---
ENABLE_VECTOR_SEARCH: bool = os.getenv("ENABLE_VECTOR_SEARCH", "true").lower() == "true"
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
EMBEDDING_DIM: int = int(os.getenv("EMBEDDING_DIM", "768"))

# RAG 검색 상위 K개
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "3"))
# 유사도 임계값 — 이 값 미만의 청크는 컨텍스트에서 제외
RAG_SIMILARITY_THRESHOLD: float = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.55"))
# RAG 청크 1개당 최대 문자 수 (SQL DDL, 에러 로그 등 긴 텍스트 고려)
RAG_CHUNK_MAX_CHARS: int = int(os.getenv("RAG_CHUNK_MAX_CHARS", "500"))
# 최근 대화 포함 메시지 수 (다자 대화 컨텍스트 고려)
RECENT_MESSAGE_COUNT: int = int(os.getenv("RECENT_MESSAGE_COUNT", "8"))
# 임베딩 최소 문자 수 — 이보다 짧은 메시지는 RAG 노이즈 방지를 위해 임베딩 생략
EMBED_MIN_CHARS: int = int(os.getenv("EMBED_MIN_CHARS", "15"))

# --- LLM 설정 ---
MAX_CONTEXT_TOKENS: int = int(os.getenv("MAX_CONTEXT_TOKENS", "6000"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
CLASSIFIER_MAX_TOKENS: int = 80
QA_MAX_TOKENS: int = 500
SUMMARY_MAX_TOKENS: int = 300

# --- 웹 검색 설정 ---
ENABLE_WEB_SEARCH: bool = os.getenv("ENABLE_WEB_SEARCH", "true").lower() == "true"
# 검색 HTTP 요청 타임아웃 (초) — 느린 검색이 답변 지연을 일으키지 않도록 짧게 유지
WEB_SEARCH_TIMEOUT: float = float(os.getenv("WEB_SEARCH_TIMEOUT", "4.0"))
# 검색 결과 최대 개수
WEB_SEARCH_TOP_K: int = int(os.getenv("WEB_SEARCH_TOP_K", "3"))
# 검색 결과 1건당 최대 문자 수
WEB_SEARCH_MAX_CHARS: int = int(os.getenv("WEB_SEARCH_MAX_CHARS", "200"))

# --- 배치 스케줄 설정 ---
SUMMARY_BATCH_HOUR: int = int(os.getenv("SUMMARY_BATCH_HOUR", "2"))
SUMMARY_BATCH_WEEKDAY: int = int(os.getenv("SUMMARY_BATCH_WEEKDAY", "0"))

# --- 로깅 설정 ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"


def validate_config() -> None:
    """필수 환경변수가 설정됐는지 검증하고 경고를 출력한다."""
    if LLM_BACKEND not in ("openrouter", "ollama"):
        raise EnvironmentError(f"LLM_BACKEND 값이 올바르지 않음: '{LLM_BACKEND}'. 'openrouter' 또는 'ollama'를 사용하세요.")

    required: dict[str, str] = {
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
        "SLACK_APP_TOKEN": SLACK_APP_TOKEN,
        "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET,
        "DATABASE_URL": DATABASE_URL,
    }
    if LLM_BACKEND == "openrouter":
        required["OPENROUTER_API_KEY"] = OPENROUTER_API_KEY

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"필수 환경변수 누락: {', '.join(missing)}")

    if not TARGET_CHANNEL_IDS:
        logger.warning("TARGET_CHANNEL_IDS가 비어 있음. app_mention 이벤트만 처리됩니다.")

    if not ENABLE_VECTOR_SEARCH:
        logger.warning("ENABLE_VECTOR_SEARCH=false. pgvector 없이 텍스트 기반 검색으로 동작합니다.")

    logger.info(
        f"설정 로드 완료 | backend={LLM_BACKEND} | classifier={CLASSIFIER_FALLBACK_CHAIN[0]} "
        f"| qa={QA_FALLBACK_CHAIN[0]} | vector_search={ENABLE_VECTOR_SEARCH} | embedding_dim={EMBEDDING_DIM}"
    )
