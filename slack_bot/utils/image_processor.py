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

    # 이미지를 1장씩 개별 호출한다.
    # 여러 장을 묶으면 n_prompt_tokens이 4096을 초과하므로 반드시 분리한다.
    # 세마포어는 호출마다 잡고 놓아 이미지 사이 간격에 QA 요청이 끼어들 수 있게 한다.
    count = len(images_b64)
    results: list[str] = []
    for i, b64 in enumerate(images_b64, 1):
        with _get_vision_semaphore():
            part = call_vision([b64], _IMAGE_DESCRIBE_PROMPT)
        if part:
            label = f"[이미지 {i}] " if count > 1 else ""
            results.append(f"{label}{part.strip()}")
        else:
            logger.warning(f"이미지 {i}/{count}장 분석 실패")

    if results:
        logger.info(f"이미지 분석 완료: {len(results)}/{count}장")
        return "\n".join(results)
    logger.warning(f"이미지 분석 전체 실패: {count}장")
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
