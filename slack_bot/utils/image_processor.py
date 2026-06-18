# Slack 첨부 이미지를 다운로드하고 LLM 전송용으로 압축하는 유틸리티
from __future__ import annotations
import base64
import io
import logging
import threading

import requests
from PIL import Image

logger = logging.getLogger(__name__)

# 텍스트 가독성을 유지하면서 토큰을 최소화하는 기준 해상도
_MAX_SIDE_PX = 1280
_JPEG_QUALITY = 85
_DOWNLOAD_TIMEOUT_SEC = 15

# analyze_slack_files에서 사용하는 이미지 분석 상수
_IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_MAX_IMAGES = 10
_IMAGE_DESCRIBE_PROMPT = (
    "이 이미지에서 텍스트를 추출해줘. "
    "서문이나 설명 없이 오류 메시지, 예외 클래스명, 스택 트레이스 라인만 한 줄씩 나열해줘. "
    "텍스트가 없으면 이미지에서 보이는 내용을 간결하게 한 줄로 설명해줘."
)

# CPU 서버에서 vision 호출이 대화형 QA와 경쟁하지 않도록 동시 실행 수를 제한한다.
_vision_sem: threading.Semaphore | None = None


def _get_vision_semaphore() -> threading.Semaphore:
    global _vision_sem
    if _vision_sem is None:
        import config
        _vision_sem = threading.Semaphore(config.MAX_CONCURRENT_VISION)
    return _vision_sem


def download_and_compress(url: str, bot_token: str) -> str | None:
    """
    Slack 비공개 이미지 URL을 다운로드하고 압축된 JPEG의 base64 문자열을 반환한다.
    실패 시 None을 반환한다.
    """
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=_DOWNLOAD_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"이미지 다운로드 실패 ({url[:60]}...): {exc}")
        return None

    content_type = resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        logger.warning(
            f"이미지 다운로드 실패: content-type={content_type!r}, "
            f"body_preview={resp.content[:200]!r}. "
            f"Slack 앱에 files:read 스코프가 누락됐을 가능성이 높습니다."
        )
        return None

    try:
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        original_size = img.size
        img = _resize_if_needed(img)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        compressed_kb = len(buf.getvalue()) // 1024
        logger.info(
            f"이미지 압축 완료: {original_size} → {img.size} / {compressed_kb}KB"
        )
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        logger.warning(f"이미지 압축 실패: {exc}")
        return None


def analyze_slack_files(files: list[dict], bot_token: str) -> str:
    """
    Slack files 목록에서 이미지를 분석하여 텍스트를 반환한다.
    이미지가 없거나 분석 실패 시 빈 문자열을 반환한다.
    세마포어로 vision 호출을 직렬화하여 CPU 서버 과부하를 방지한다.
    """
    from services.llm_service import call_vision

    images_b64: list[str] = []
    for f in files:
        if len(images_b64) >= _MAX_IMAGES:
            break
        mime = f.get("mimetype", "")
        if not any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES):
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        b64 = download_and_compress(url, bot_token)
        if b64:
            images_b64.append(b64)

    if not images_b64:
        return ""

    count = len(images_b64)
    if count == 1:
        prompt = _IMAGE_DESCRIBE_PROMPT
    else:
        prompt = (
            f"첨부된 이미지 {count}장을 순서대로 분석해줘. "
            "각 이미지마다 '[이미지 N]' 레이블을 붙여 구분해줘. "
            "서문이나 설명 없이 오류 메시지, 예외 클래스명, 스택 트레이스 라인만 한 줄씩 나열해줘. "
            "텍스트가 없으면 해당 이미지에서 보이는 내용을 간결하게 한 줄로 설명해줘."
        )

    with _get_vision_semaphore():
        result = call_vision(images_b64, prompt)

    if result:
        logger.info(f"이미지 분석 완료: {count}장")
        return result.strip()
    logger.warning(f"이미지 분석 실패: {count}장")
    return ""


def _resize_if_needed(img: Image.Image) -> Image.Image:
    """긴 쪽이 _MAX_SIDE_PX를 초과하면 비율을 유지하며 축소한다."""
    w, h = img.size
    if max(w, h) <= _MAX_SIDE_PX:
        return img
    if w >= h:
        new_w = _MAX_SIDE_PX
        new_h = int(h * _MAX_SIDE_PX / w)
    else:
        new_h = _MAX_SIDE_PX
        new_w = int(w * _MAX_SIDE_PX / h)
    return img.resize((new_w, new_h), Image.LANCZOS)
