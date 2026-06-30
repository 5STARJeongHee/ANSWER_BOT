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
# OCR 담당: 텍스트 밀도가 높은 이미지(로그·코드·에러). Vision LLM은 비텍스트 화면을 담당.
_IMAGE_DESCRIBE_PROMPT = (
    "이 이미지가 어떤 화면인지 분석해줘.\n"
    "1. UI/애플리케이션 화면이라면 현재 어떤 기능을 실행 중인지, 어떤 상태(오류·로딩·완료 등)인지 설명해줘.\n"
    "2. 설정 화면이라면 주요 설정값과 현재 상태를 설명해줘.\n"
    "3. 그래프·차트·다이어그램이라면 내용을 요약해줘.\n"
    "서문 없이 본론부터 화면 상황을 설명해줘."
)
# OCR 텍스트가 이 글자 수 이상이면 텍스트 밀도가 높은 이미지로 판단, Vision LLM 호출 생략
_OCR_TEXT_THRESHOLD = 150

# CPU 서버에서 vision 호출이 대화형 QA와 경쟁하지 않도록 동시 실행 수를 제한한다.
_vision_sem: threading.Semaphore | None = None

# RapidOCR 리더 싱글턴 (첫 호출 시 초기화)
_ocr_reader = None
_ocr_reader_lock = threading.Lock()


def _get_ocr_reader():
    """RapidOCR 리더를 초기화하고 반환한다. 미설치 시 None을 반환한다."""
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_reader_lock:
            if _ocr_reader is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    _ocr_reader = RapidOCR()
                    logger.info("RapidOCR 리더 초기화 완료")
                except ImportError:
                    logger.info("rapidocr-onnxruntime 미설치 — OCR 비활성화, Vision LLM으로 폴백")
                    _ocr_reader = False  # 재시도 방지 sentinel
    return _ocr_reader if _ocr_reader is not False else None


def _extract_text_by_ocr(b64_jpeg: str) -> str:
    """b64 인코딩된 JPEG에서 RapidOCR로 텍스트를 추출한다. 실패 시 빈 문자열 반환."""
    reader = _get_ocr_reader()
    if reader is None:
        return ""
    try:
        import numpy as np
        image_bytes = base64.b64decode(b64_jpeg)
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        result, _ = reader(np.array(img))
        if not result:
            return ""
        lines = [item[1] for item in result if item[2] >= 0.5]
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"OCR 텍스트 추출 실패: {exc}")
        return ""


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
            label = f"[이미지 {i}] " if count > 1 else ""

            # OCR 먼저 시도: 텍스트 밀도가 높으면(로그·코드·에러) OCR 결과 사용
            ocr_text = _extract_text_by_ocr(b64)
            if len(ocr_text) >= _OCR_TEXT_THRESHOLD:
                logger.info(f"이미지 {i}/{count}: OCR 완료 ({len(ocr_text)}자)")
                image_results.append(f"{label}{ocr_text.strip()}")
                continue

            # 텍스트 부족 → Vision LLM으로 화면 분석
            with _get_vision_semaphore():
                part = call_vision([b64], _IMAGE_DESCRIBE_PROMPT)
            if part:
                image_results.append(f"{label}{part.strip()}")
            else:
                logger.warning(f"이미지 {i}/{count}장 분석 실패")

        if image_results:
            logger.info(f"이미지 분석 완료: {len(image_results)}/{count}장")
            logger.debug(f"이미지 분석 결과(앞 500자):\n{chr(10).join(image_results)[:500]}")
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
