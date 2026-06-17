# Slack 첨부 이미지를 다운로드하고 LLM 전송용으로 압축하는 유틸리티
from __future__ import annotations
import base64
import io
import logging

import requests
from PIL import Image

logger = logging.getLogger(__name__)

# 텍스트 가독성을 유지하면서 토큰을 최소화하는 기준 해상도
_MAX_SIDE_PX = 1280
_JPEG_QUALITY = 85
_DOWNLOAD_TIMEOUT_SEC = 15


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
