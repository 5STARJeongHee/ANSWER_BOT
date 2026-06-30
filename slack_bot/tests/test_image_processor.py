# image_processor 단위 테스트 — OCR 라우팅 및 헬퍼 함수 검증
import sys
from unittest.mock import patch, MagicMock

# PIL / requests는 모듈 임포트 시점에 필요하므로 먼저 stub 등록
_pil_stub = MagicMock()
sys.modules.setdefault("PIL", _pil_stub)
sys.modules.setdefault("PIL.Image", _pil_stub)

_requests_stub = MagicMock()
sys.modules.setdefault("requests", _requests_stub)

# 지연 임포트 대상도 미리 등록해 두면 patch 경로가 안정적으로 잡힌다.
_llm_stub = MagicMock()
_llm_stub.call_vision = MagicMock(return_value="Vision LLM 결과")
_services_pkg = MagicMock()
sys.modules.setdefault("services", _services_pkg)
sys.modules.setdefault("services.llm_service", _llm_stub)

# conftest.py의 clear_classifier_cache fixture가 services.classifier._classify_cache를 참조한다.
_classifier_stub = MagicMock()
_classifier_stub._classify_cache = {}
sys.modules.setdefault("services.classifier", _classifier_stub)

_file_proc_stub = MagicMock()
_file_proc_stub.extract_file_texts = MagicMock(return_value="")
sys.modules.setdefault("utils.file_processor", _file_proc_stub)

_config_stub = MagicMock()
_config_stub.MAX_CONCURRENT_VISION = 2
sys.modules.setdefault("config", _config_stub)

import pytest
import utils.image_processor as ip
from utils.image_processor import (
    analyze_slack_files,
    _extract_text_by_ocr,
    _OCR_TEXT_THRESHOLD,
    _MAX_SIDE_OCR_PX,
    _MAX_SIDE_VLM_PX,
)

_FAKE_RAW = b"fake_jpeg_bytes"
_FAKE_B64 = "ZmFrZV9qcGVnX2J5dGVz"  # base64("fake_jpeg_bytes")


@pytest.fixture(autouse=True)
def reset_ocr_reader():
    """각 테스트 전 _ocr_reader 전역 상태를 초기화한다."""
    ip._ocr_reader = None
    yield
    ip._ocr_reader = None


def _image_file(url="https://example.com/img.png", mime="image/png"):
    return {"mimetype": mime, "url_private": url}


class TestGetOcrReader:
    def test_returns_reader_on_successful_import(self):
        mock_reader = MagicMock()
        mock_rapidocr = MagicMock()
        mock_rapidocr.RapidOCR.return_value = mock_reader

        with patch.dict(sys.modules, {"rapidocr_onnxruntime": mock_rapidocr}):
            ip._ocr_reader = None
            result = ip._get_ocr_reader()

        assert result is mock_reader

    def test_returns_none_on_import_error(self):
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": None}):
            ip._ocr_reader = None
            result = ip._get_ocr_reader()

        assert result is None

    def test_caches_reader_on_second_call(self):
        mock_reader = MagicMock()
        mock_rapidocr = MagicMock()
        mock_rapidocr.RapidOCR.return_value = mock_reader

        with patch.dict(sys.modules, {"rapidocr_onnxruntime": mock_rapidocr}):
            ip._ocr_reader = None
            ip._get_ocr_reader()
            ip._get_ocr_reader()

        assert mock_rapidocr.RapidOCR.call_count == 1


class TestExtractTextByOcr:
    def test_returns_empty_when_reader_unavailable(self):
        with patch("utils.image_processor._get_ocr_reader", return_value=None):
            result = _extract_text_by_ocr(_FAKE_RAW)
        assert result == ""

    def test_returns_joined_text_above_confidence(self):
        """신뢰도 0.5 이상인 텍스트만 줄바꿈으로 연결해 반환한다."""
        mock_reader = MagicMock()
        mock_reader.return_value = (
            [
                [None, "ERROR: Connection refused", 0.92],
                [None, "at com.example.Main.run", 0.78],
                [None, "low_conf_noise", 0.3],  # 임계값 미만 — 제외
            ],
            0.01,
        )
        mock_img = MagicMock()
        mock_img.convert.return_value = mock_img
        mock_np = MagicMock()
        mock_np.array.return_value = MagicMock()

        with patch("utils.image_processor._get_ocr_reader", return_value=mock_reader), \
             patch("utils.image_processor.Image") as mock_pil, \
             patch("utils.image_processor._resize_if_needed", return_value=mock_img), \
             patch.dict(sys.modules, {"numpy": mock_np}):
            mock_pil.open.return_value = mock_img
            result = _extract_text_by_ocr(_FAKE_RAW)

        assert "ERROR: Connection refused" in result
        assert "at com.example.Main.run" in result
        assert "low_conf_noise" not in result

    def test_uses_ocr_resolution_for_resize(self):
        """OCR 전에 _MAX_SIDE_OCR_PX 기준으로 축소한다."""
        mock_reader = MagicMock()
        mock_reader.return_value = ([], 0.0)
        mock_img = MagicMock()
        mock_img.convert.return_value = mock_img
        mock_np = MagicMock()

        with patch("utils.image_processor._get_ocr_reader", return_value=mock_reader), \
             patch("utils.image_processor.Image") as mock_pil, \
             patch("utils.image_processor._resize_if_needed", return_value=mock_img) as mock_resize, \
             patch.dict(sys.modules, {"numpy": mock_np}):
            mock_pil.open.return_value = mock_img
            _extract_text_by_ocr(_FAKE_RAW)

        mock_resize.assert_called_once_with(mock_img, _MAX_SIDE_OCR_PX)

    def test_returns_empty_when_ocr_result_is_none(self):
        mock_reader = MagicMock()
        mock_reader.return_value = (None, 0.0)
        mock_img = MagicMock()
        mock_img.convert.return_value = mock_img
        mock_np = MagicMock()

        with patch("utils.image_processor._get_ocr_reader", return_value=mock_reader), \
             patch("utils.image_processor.Image") as mock_pil, \
             patch("utils.image_processor._resize_if_needed", return_value=mock_img), \
             patch.dict(sys.modules, {"numpy": mock_np}):
            mock_pil.open.return_value = mock_img
            result = _extract_text_by_ocr(_FAKE_RAW)

        assert result == ""

    def test_returns_empty_on_exception(self):
        mock_reader = MagicMock()
        mock_reader.side_effect = RuntimeError("OCR 내부 오류")
        mock_img = MagicMock()
        mock_img.convert.return_value = mock_img
        mock_np = MagicMock()

        with patch("utils.image_processor._get_ocr_reader", return_value=mock_reader), \
             patch("utils.image_processor.Image") as mock_pil, \
             patch("utils.image_processor._resize_if_needed", return_value=mock_img), \
             patch.dict(sys.modules, {"numpy": mock_np}):
            mock_pil.open.return_value = mock_img
            result = _extract_text_by_ocr(_FAKE_RAW)

        assert result == ""


class TestAnalyzeSlackFilesOcrRouting:
    """OCR-first 라우팅: 텍스트 밀도에 따라 OCR 또는 Vision LLM 선택."""

    def test_uses_ocr_result_and_skips_vision_when_text_sufficient(self):
        """OCR 추출 텍스트가 임계값 이상이면 Vision LLM을 호출하지 않는다."""
        long_text = "ERROR: Connection refused\n" * 10
        assert len(long_text) >= _OCR_TEXT_THRESHOLD

        with patch("utils.image_processor._download_raw", return_value=_FAKE_RAW), \
             patch("utils.image_processor._extract_text_by_ocr", return_value=long_text), \
             patch.object(_llm_stub, "call_vision") as mock_cv:
            result = analyze_slack_files([_image_file()], "tok")

        mock_cv.assert_not_called()
        assert "Connection refused" in result

    def test_delegates_to_vision_when_ocr_text_insufficient(self):
        """OCR 텍스트가 임계값 미만이면 Vision LLM을 호출하고 그 결과를 반환한다."""
        short_text = ""

        with patch("utils.image_processor._download_raw", return_value=_FAKE_RAW), \
             patch("utils.image_processor._extract_text_by_ocr", return_value=short_text), \
             patch("utils.image_processor._compress_to_b64", return_value=_FAKE_B64), \
             patch.object(_llm_stub, "call_vision", return_value="로그인 화면: 오류 상태") as mock_cv:
            result = analyze_slack_files([_image_file()], "tok")

        mock_cv.assert_called_once()
        assert "로그인 화면" in result

    def test_partial_ocr_text_appended_to_vision_result(self):
        """OCR이 부분 텍스트를 찾았을 때(임계값 미만) Vision LLM 결과에 보완으로 포함한다."""
        partial_text = "NullPointerException"  # 짧지만 의미 있는 텍스트

        with patch("utils.image_processor._download_raw", return_value=_FAKE_RAW), \
             patch("utils.image_processor._extract_text_by_ocr", return_value=partial_text), \
             patch("utils.image_processor._compress_to_b64", return_value=_FAKE_B64), \
             patch.object(_llm_stub, "call_vision", return_value="오류 다이얼로그 화면"):
            result = analyze_slack_files([_image_file()], "tok")

        assert "오류 다이얼로그 화면" in result
        assert "NullPointerException" in result

    def test_vision_compress_uses_vlm_resolution(self):
        """Vision LLM 경로에서 _MAX_SIDE_VLM_PX 해상도로 압축한다."""
        with patch("utils.image_processor._download_raw", return_value=_FAKE_RAW), \
             patch("utils.image_processor._extract_text_by_ocr", return_value=""), \
             patch("utils.image_processor._compress_to_b64", return_value=_FAKE_B64) as mock_compress, \
             patch.object(_llm_stub, "call_vision", return_value="화면 분석"):
            analyze_slack_files([_image_file()], "tok")

        mock_compress.assert_called_once_with(_FAKE_RAW, _MAX_SIDE_VLM_PX)

    def test_skips_non_image_files(self):
        """이미지가 아닌 파일은 Vision LLM과 OCR 모두 건너뛴다."""
        non_image = {"mimetype": "application/pdf", "url_private": "https://example.com/doc.pdf"}

        with patch("utils.image_processor._download_raw") as mock_dl:
            analyze_slack_files([non_image], "tok")

        mock_dl.assert_not_called()

    def test_multiple_images_ocr_first_vision_fallback(self):
        """여러 이미지: 첫 번째는 OCR(텍스트 충분), 두 번째는 Vision LLM(텍스트 부족)."""
        ocr_texts = ["a" * 200, ""]

        with patch("utils.image_processor._download_raw", return_value=_FAKE_RAW), \
             patch("utils.image_processor._extract_text_by_ocr", side_effect=ocr_texts), \
             patch("utils.image_processor._compress_to_b64", return_value=_FAKE_B64), \
             patch.object(_llm_stub, "call_vision", return_value="Vision 결과") as mock_cv:
            result = analyze_slack_files([_image_file(), _image_file()], "tok")

        mock_cv.assert_called_once()
        assert "Vision 결과" in result

    def test_download_failure_skips_image(self):
        """이미지 다운로드 실패 시 해당 이미지를 건너뛴다."""
        with patch("utils.image_processor._download_raw", return_value=None), \
             patch.object(_llm_stub, "call_vision") as mock_cv:
            result = analyze_slack_files([_image_file()], "tok")

        mock_cv.assert_not_called()
        assert result == ""

    def test_compress_failure_skips_vision(self):
        """VLM 압축 실패 시 해당 이미지를 건너뛴다."""
        with patch("utils.image_processor._download_raw", return_value=_FAKE_RAW), \
             patch("utils.image_processor._extract_text_by_ocr", return_value=""), \
             patch("utils.image_processor._compress_to_b64", return_value=None), \
             patch.object(_llm_stub, "call_vision") as mock_cv:
            result = analyze_slack_files([_image_file()], "tok")

        mock_cv.assert_not_called()
        assert result == ""
