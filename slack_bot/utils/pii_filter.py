# 개인정보(PII)를 감지하고 마스킹 처리하는 유틸리티 모듈
import re

# 이메일 주소 패턴
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# 한국 전화번호 패턴 (010-XXXX-XXXX, 02-XXXX-XXXX, 0XX-XXX-XXXX 등)
_PHONE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(0(?:10|1[1-9]|2|[3-9]\d)"   # 지역번호/휴대폰 번호 앞자리
    r"[-\s]?"
    r"\d{3,4}"
    r"[-\s]?"
    r"\d{4})"
    r"(?!\d)"
)

# 주민등록번호 패턴 (XXXXXX-XXXXXXX)
_RRN_PATTERN = re.compile(
    r"(?<!\d)\d{6}[-\s]\d{7}(?!\d)"
)

# 신용카드 번호 패턴 (XXXX-XXXX-XXXX-XXXX)
_CARD_PATTERN = re.compile(
    r"(?<!\d)\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}(?!\d)"
)

# IP 주소 패턴 (내부 IP 포함)
_IP_PATTERN = re.compile(
    r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"
)


def mask_email(text: str) -> str:
    """텍스트에서 이메일 주소를 마스킹한다."""
    return _EMAIL_PATTERN.sub("[EMAIL_REMOVED]", text)


def mask_phone(text: str) -> str:
    """텍스트에서 한국 전화번호를 마스킹한다."""
    return _PHONE_PATTERN.sub("[PHONE_REMOVED]", text)


def mask_rrn(text: str) -> str:
    """텍스트에서 주민등록번호를 마스킹한다."""
    return _RRN_PATTERN.sub("[RRN_REMOVED]", text)


def mask_card(text: str) -> str:
    """텍스트에서 카드 번호를 마스킹한다."""
    return _CARD_PATTERN.sub("[CARD_REMOVED]", text)


def mask_ip(text: str) -> str:
    """텍스트에서 IP 주소를 마스킹한다."""
    return _IP_PATTERN.sub("[IP_REMOVED]", text)


def apply_pii_filter(text: str) -> str:
    """
    텍스트에 모든 PII 마스킹을 순서대로 적용한다.
    저장 전 항상 이 함수를 거쳐야 한다.
    """
    if not text:
        return text

    text = mask_rrn(text)     # 주민번호를 가장 먼저 (숫자 패턴 충돌 방지)
    text = mask_card(text)
    text = mask_email(text)
    text = mask_phone(text)
    text = mask_ip(text)

    return text


def has_pii(text: str) -> bool:
    """텍스트에 PII가 포함되어 있는지 여부를 반환한다 (감사 로깅용)."""
    if not text:
        return False
    patterns = [_EMAIL_PATTERN, _PHONE_PATTERN, _RRN_PATTERN, _CARD_PATTERN]
    return any(p.search(text) for p in patterns)
