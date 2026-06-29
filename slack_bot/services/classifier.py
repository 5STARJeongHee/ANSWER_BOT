# Slack 메시지를 질문/요청/기타로 분류하는 경량 LLM 기반 분류기
from __future__ import annotations
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import json

from services.llm_service import call_classifier, parse_json_response
import config

logger = logging.getLogger(__name__)

# Redis 클라이언트 초기화
_redis_client = None
if config.REDIS_URL:
    try:
        import redis
        _redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
        # Test connection
        _redis_client.ping()
        logger.info(f"Redis 캐시 연동 성공: {config.REDIS_URL}")
    except Exception as exc:
        logger.warning(f"Redis 연결 실패, 인메모리 캐시로 대체합니다: {exc}")
        _redis_client = None

# 분류 캐시 (동일 메시지 중복 호출 방지)
_classify_cache: dict[str, "ClassifyResult"] = {}
_CACHE_MAX_SIZE = 256


class MessageCategory(str, Enum):
    QUESTION = "QUESTION"    # 정보/방법/상태를 묻는 질문
    REQUEST = "REQUEST"      # 특정 작업/처리를 부탁하는 요청
    NONE = "NONE"            # 잡담, 공지, 감사 인사 등


@dataclass
class ClassifyResult:
    category: MessageCategory
    confidence: float
    reason: str
    is_actionable: bool  # QUESTION 또는 REQUEST이면 True

    @classmethod
    def none_result(cls) -> "ClassifyResult":
        return cls(
            category=MessageCategory.NONE,
            confidence=1.0,
            reason="분류 불필요",
            is_actionable=False,
        )

    def to_json(self) -> str:
        return json.dumps({
            "category": self.category.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "is_actionable": self.is_actionable,
        })

    @classmethod
    def from_json(cls, data: str) -> "ClassifyResult":
        parsed = json.loads(data)
        return cls(
            category=MessageCategory(parsed["category"]),
            confidence=parsed["confidence"],
            reason=parsed["reason"],
            is_actionable=parsed["is_actionable"],
        )


_SYSTEM_PROMPT = (
    "너는 사내 Slack 메시지 분류기다. "
    "입력 메시지가 질문(QUESTION), 업무 요청(REQUEST), 해당 없음(NONE) 중 무엇인지 판단한다.\n\n"
    "분류 기준:\n"
    "- QUESTION: 챗봇 또는 담당자에게 직접 정보·방법·상태를 묻는 문장\n"
    "- REQUEST: 챗봇 또는 담당자에게 직접 특정 작업·처리·검토를 부탁하는 문장\n"
    "- NONE: 잡담, 공지, 감사 인사, 단순 반응, 완료 보고, "
    "다른 사람에게 답하거나 상황을 설명하는 메시지\n\n"
    "주의: '배포 완료했습니다', '올렸습니다', '처리했습니다', '패치 올렸습니다', "
    "'[문서번호: NNN]' 형태의 배포 이력 메시지, '워크스트림에 올렸습니다' 등 "
    "완료 보고 형태의 메시지는 NONE이다. "
    "누군가에게 직접 답변하거나 상황을 설명하는 형태도 NONE이다.\n"
    'JSON만 출력: {"category": "QUESTION|REQUEST|NONE", "confidence": 0.0~1.0, "reason": "이유"}'
)


def _is_bot_message_by_heuristic(message: str) -> bool:
    """봇 자신의 응답 패턴인지 휴리스틱으로 판단한다."""
    bot_prefixes = [
        "안녕하세요! 사내 Q&A 봇입니다",
        "답변을 생성 중입니다",
        "죄송합니다, 현재 답변을 생성할 수 없습니다",
        "[AI 생성 답변]",
        "담당자에게 문의해 주세요",
    ]
    return any(message.startswith(p) for p in bot_prefixes)


def classify_message(
    message: str,
    is_mention: bool = False,
    bot_user_id: Optional[str] = None,
    sender_user_id: Optional[str] = None,
) -> ClassifyResult:
    """
    메시지를 분류한다.
    - 봇 메시지는 NONE으로 즉시 반환한다.
    - 앱 멘션(@챗봇)은 항상 QUESTION으로 처리한다.
    - 나머지는 LLM 분류기를 호출한다.
    """
    if not message or not message.strip():
        return ClassifyResult.none_result()

    # 봇 자신의 메시지 필터링
    if bot_user_id and sender_user_id and bot_user_id == sender_user_id:
        return ClassifyResult.none_result()
    if _is_bot_message_by_heuristic(message):
        return ClassifyResult.none_result()

    # 앱 멘션은 항상 처리
    if is_mention:
        return ClassifyResult(
            category=MessageCategory.QUESTION,
            confidence=1.0,
            reason="앱 멘션 이벤트",
            is_actionable=True,
        )

    # 캐시 조회
    cache_key = f"classify:{message[:200]}"  # 200자까지만 키로 사용
    if _redis_client:
        try:
            cached_val = _redis_client.get(cache_key)
            if cached_val:
                logger.debug(f"Redis 분류 캐시 히트: {cache_key[:50]!r}")
                return ClassifyResult.from_json(cached_val)
        except Exception as exc:
            logger.warning(f"Redis 캐시 읽기 실패: {exc}")
    else:
        if cache_key in _classify_cache:
            logger.debug(f"인메모리 분류 캐시 히트: {cache_key[:50]!r}")
            return _classify_cache[cache_key]

    # LLM 분류 호출
    llm_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"메시지: {message}"},
    ]

    raw = call_classifier(llm_messages)
    parsed = parse_json_response(
        raw or "",
        default={"category": "NONE", "confidence": 0.0, "reason": "분류 실패"},
    )

    try:
        category = MessageCategory(parsed.get("category", "NONE").upper())
    except ValueError:
        category = MessageCategory.NONE

    result = ClassifyResult(
        category=category,
        confidence=float(parsed.get("confidence", 0.0)),
        reason=str(parsed.get("reason", "")),
        is_actionable=category in (MessageCategory.QUESTION, MessageCategory.REQUEST),
    )

    # 캐시 저장 (Redis는 TTL 7일, 인메모리는 크기 제한)
    if _redis_client:
        try:
            _redis_client.setex(cache_key, 604800, result.to_json()) # 7 days
        except Exception as exc:
            logger.warning(f"Redis 캐시 저장 실패: {exc}")
    else:
        if len(_classify_cache) >= _CACHE_MAX_SIZE:
            # 가장 오래된 항목 제거 (FIFO)
            oldest_key = next(iter(_classify_cache))
            del _classify_cache[oldest_key]
        _classify_cache[cache_key] = result

    logger.info(
        f"분류 결과: {category.value} (신뢰도={result.confidence:.2f}) | "
        f"메시지={message[:50]!r}"
    )
    return result


# ---------------------------------------------------------------------------
# 주제 태그 추출
# ---------------------------------------------------------------------------

_TOPIC_SYSTEM_PROMPT = (
    "너는 사내 Slack 대화에서 핵심 주제를 추출하는 태거다. "
    "입력 메시지의 핵심 주제를 한국어 2~5단어로 추출하라. "
    "구체적인 기술명·업무 영역·오류 유형을 포함하라. "
    '예시: "Redis 연결 오류", "Docker 배포 실패", "API 인증 처리", "DB 마이그레이션 방법"\n'
    'JSON만 출력: {"topic": "핵심 주제"}'
)


def extract_topic(message: str) -> Optional[str]:
    """메시지에서 2~5단어 핵심 주제 태그를 추출한다. 실패 또는 너무 짧은 메시지이면 None을 반환한다."""
    if not message or len(message.strip()) < 10:
        return None

    llm_messages = [
        {"role": "system", "content": _TOPIC_SYSTEM_PROMPT},
        {"role": "user", "content": f"메시지: {message[:500]}"},
    ]
    raw = call_classifier(llm_messages)
    parsed = parse_json_response(raw or "", default={"topic": ""})
    topic = str(parsed.get("topic", "")).strip()
    if topic:
        logger.debug(f"주제 추출: {topic!r} | 메시지={message[:50]!r}")
    return topic if topic else None


def extract_topic_and_product(
    message: str,
    products: list[dict],
) -> tuple[Optional[str], Optional[str]]:
    """메시지에서 주제와 관련 제품 키를 한 번의 LLM 호출로 추출한다.
    products: [{"key": "iruda_backend", "name": "이루다 백엔드", "aliases": [...]}]
    반환: (topic, product_key) — 분류 불가 시 각각 None.
    """
    if not message or len(message.strip()) < 10:
        return None, None

    if products:
        products_str = "\n".join(
            f"- {p['key']}: {p['name']} (별칭: {', '.join(p['aliases'][:5])})"
            for p in products
        )
    else:
        products_str = "(등록된 제품 없음)"

    system_prompt = (
        "너는 사내 Slack 대화에서 핵심 주제와 관련 제품을 분류하는 분석기다.\n"
        "핵심 주제를 한국어 2~5단어로 추출하고, "
        "아래 제품 목록 중 메시지에 명확히 언급된 제품이 있으면 product_key를 반환하라. "
        "제품이 불확실하거나 언급이 없으면 product는 null로 반환하라.\n\n"
        f"알려진 제품 목록:\n{products_str}\n\n"
        'JSON만 출력: {"topic": "핵심 주제", "product": "product_key 또는 null"}'
    )

    llm_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"메시지: {message[:500]}"},
    ]
    raw = call_classifier(llm_messages)
    parsed = parse_json_response(raw or "", default={"topic": "", "product": None})

    topic = str(parsed.get("topic", "")).strip() or None
    product = parsed.get("product")
    if not isinstance(product, str) or product.lower() in ("null", "none", ""):
        product = None

    # LLM이 알려진 제품 키 목록에 없는 값을 반환하면 무시
    valid_keys = {p["key"] for p in products}
    if product and product not in valid_keys:
        logger.warning(f"LLM이 알 수 없는 제품 키 반환 (무시): {product!r}")
        product = None

    logger.debug(f"주제+제품 추출: topic={topic!r} product={product!r} | 메시지={message[:50]!r}")
    return topic, product
