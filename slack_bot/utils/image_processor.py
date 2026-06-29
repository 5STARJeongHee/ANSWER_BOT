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
    "이 이미지의 전반적인 내용과 화면 구성을 상세히 설명해줘.\n"
    "1. UI/UX 화면이라면 현재 어떤 메뉴/기능을 실행 중인지, 어떤 상태(오류, 로딩, 완료 등)인지 구체적으로 묘사해줘.\n"
    "2. 로그, 콘솔, 코드 화면이라면 오류 메시지, HTTP 상태코드, 예외 클래스명, 스택 트레이스를 명확하게 추출해줘.\n"
    "3. 기타 설정 화면이라면 주요 설정값과 상태를 설명해줘.\n"
    "불필요한 서문 없이 본론부터 화면의 상황을 분석하듯 설명해줘."
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
    Slack files 목록에서 이미지를 분석하고, 비이미지 파일(txt, xlsx, docx, pdf, mov 등)의
    텍스트도 함께 추출하여 반환한다.
    결과가 없으면 빈 문자열을 반환한다.
    """
    from services.llm_service import call_vision
    from utils.file_processor import extract_file_texts

    # ── 이미지: vision 모델로 분석 ──────────────────────────────────────────
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

    image_results: list[str] = []
    if images_b64:
        # 이미지를 1장씩 개별 호출한다.
        # 여러 장을 묶으면 n_prompt_tokens이 4096을 초과하므로 반드시 분리한다.
        # 세마포어는 호출마다 잡고 놓아 이미지 사이 간격에 QA 요청이 끼어들 수 있게 한다.
        count = len(images_b64)
        for i, b64 in enumerate(images_b64, 1):
            with _get_vision_semaphore():
                part = call_vision([b64], _IMAGE_DESCRIBE_PROMPT)
            if part:
                label = f"[이미지 {i}] " if count > 1 else ""
                image_results.append(f"{label}{part.strip()}")
            else:
                logger.warning(f"이미지 {i}/{count}장 분석 실패")

        if image_results:
            logger.info(f"이미지 분석 완료: {len(image_results)}/{count}장")
        else:
            logger.warning(f"이미지 분석 전체 실패: {count}장")

    # ── 비이미지 파일: 텍스트 추출 ─────────────────────────────────────────
    file_text = extract_file_texts(files, bot_token)
    if file_text:
        logger.info(f"비이미지 파일 텍스트 추출 완료 ({len(file_text)}자)")

    # ── 결과 통합 ─────────────────────────────────────────────────────────
    parts = [p for p in ["\n".join(image_results), file_text] if p]
    return "\n\n".join(parts) if parts else ""


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
