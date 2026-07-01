# 파일 분석 결과를 담는 공용 dataclass — image_processor와 file_processor가 공유
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentResult:
    """Slack 첨부파일 1개에 대한 분석 결과."""

    slack_file_id: str  # Slack file dict의 "id" 필드, 없으면 ""
    file_name: str      # 파일명 ("name" 필드)
    mime_type: str      # MIME 타입 ("mimetype" 필드)
    file_type: str      # "image" | "text" | "xlsx" | "docx" | "pdf" | "video" | "other"
    analysis_text: str  # OCR / Vision / 텍스트 추출 결과 (위치 레이블 없는 순수 텍스트)
