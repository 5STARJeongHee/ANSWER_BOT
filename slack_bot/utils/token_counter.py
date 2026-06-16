# LLM 프롬프트 토큰 수를 추정하는 유틸리티 모듈
from __future__ import annotations
import re

# 한글 유니코드 범위
_KOREAN_PATTERN = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")

# 영어 단어 패턴
_WORD_PATTERN = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    """
    텍스트의 토큰 수를 추정한다.
    한국어는 문자 기반(약 2자당 1토큰), 영어는 단어 기반(1.3 계수) 혼합 방식을 사용한다.
    실제 토큰 수보다 약 10~15% 과추정하여 안전 마진을 확보한다.
    """
    if not text:
        return 0

    korean_chars = len(_KOREAN_PATTERN.findall(text))
    non_korean = _KOREAN_PATTERN.sub("", text)
    english_words = len(_WORD_PATTERN.findall(non_korean))

    # 한국어: 약 2글자당 1토큰 (BPE 특성상 한글은 토큰 효율이 낮음)
    korean_tokens = int(korean_chars / 1.5)
    # 영어: 단어 수 × 1.3 (서브워드 분할 고려)
    english_tokens = int(english_words * 1.3)

    return max(1, korean_tokens + english_tokens)


def estimate_message_tokens(messages: list[dict]) -> int:
    """메시지 목록 전체의 토큰 수를 추정한다."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        # 메시지 구조 오버헤드 (~4토큰/메시지)
        total += 4
    # 응답 프라이밍 오버헤드
    total += 3
    return total


def trim_messages_to_budget(
    messages: list[dict],
    system_prompt: str,
    max_tokens: int = 6000,
    keep_last_n: int = 1,
) -> list[dict]:
    """
    토큰 예산 내에 맞도록 오래된 메시지를 제거한다.
    keep_last_n개의 최신 메시지는 항상 보존한다.
    """
    if not messages:
        return messages

    system_tokens = estimate_tokens(system_prompt)
    budget = max_tokens - system_tokens

    # 반드시 보존할 최신 메시지
    protected = messages[-keep_last_n:] if len(messages) >= keep_last_n else messages[:]
    optional = messages[:-keep_last_n] if len(messages) > keep_last_n else []

    protected_tokens = estimate_message_tokens(protected)
    remaining_budget = budget - protected_tokens

    if remaining_budget <= 0:
        return protected

    # 예산 범위 내에서 최신 optional 메시지부터 포함
    result = []
    for msg in reversed(optional):
        tokens = estimate_tokens(msg.get("content", ""))
        if remaining_budget - tokens < 0:
            break
        result.insert(0, msg)
        remaining_budget -= tokens

    return result + protected
