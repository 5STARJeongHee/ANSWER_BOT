# web_search 서비스 단위 테스트 — DuckDuckGo / SearXNG 실제 호출 없이 검증
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# test_event_handler.py가 먼저 실행됐을 때 등록된 stub을 제거해 실제 모듈을 로드한다.
sys.modules.pop("services.web_search", None)

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import services.web_search as ws

from unittest.mock import patch as _patch

import httpx

from services.web_search import (
    should_search,
    search_web,
    format_web_search_for_prompt,
    SearchResult,
    SearXNGProvider,
)


def _web_search_config(**overrides):
    """web_search 모듈의 config를 교체하는 context manager."""
    defaults = {
        "ENABLE_WEB_SEARCH": True,
        "WEB_SEARCH_TOP_K": 3,
        "WEB_SEARCH_TIMEOUT": 4.0,
        "WEB_SEARCH_MAX_CHARS": 200,
    }
    defaults.update(overrides)
    mock_cfg = MagicMock()
    for k, v in defaults.items():
        setattr(mock_cfg, k, v)
    return patch.object(ws, "config", mock_cfg)


class TestShouldSearch:
    def test_true_when_image_marker_present(self):
        assert should_search("[첨부 이미지 분석]\n에러 내용") is True

    def test_true_when_error_log_pattern(self):
        assert should_search("Traceback (most recent call last):") is True

    def test_true_when_java_exception(self):
        assert should_search("java.lang.NullPointerException at com.example") is True

    def test_true_for_general_question(self):
        assert should_search("내일 날씨가 어때?") is True

    def test_false_for_short_greeting(self):
        assert should_search("응?") is False

    def test_false_for_empty(self):
        assert should_search("") is False


class TestFormatWebSearchForPrompt:
    def test_empty_input_returns_empty(self):
        assert format_web_search_for_prompt("") == ""

    def test_wraps_with_header(self):
        result = format_web_search_for_prompt("검색 결과 내용")
        assert result.startswith("[웹 검색 결과]")
        assert "검색 결과 내용" in result


class TestSearchWeb:
    def _make_provider(self, results):
        provider = MagicMock()
        provider.search = MagicMock(return_value=results)
        return provider

    def test_returns_empty_when_disabled(self):
        with _web_search_config(ENABLE_WEB_SEARCH=False):
            result = search_web("[첨부 이미지 분석]\n오류 로그")
        assert result == ""

    def test_returns_empty_when_should_not_search(self):
        with _web_search_config():
            result = search_web("응?")
        assert result == ""

    def test_returns_formatted_results(self):
        provider = self._make_provider([
            SearchResult(
                title="Spring 오류 해결",
                body="DefaultDeserializer 설정을 확인하세요.",
                url="https://example.com",
            ),
        ])
        with _web_search_config():
            result = search_web("[첨부 이미지 분석]\njava.io.IOException", provider=provider)
        assert "Spring 오류 해결" in result
        assert "DefaultDeserializer" in result

    def test_returns_empty_on_no_results(self):
        provider = self._make_provider([])
        with _web_search_config():
            result = search_web("[첨부 이미지 분석]\n오류", provider=provider)
        assert result == ""

    def test_body_is_capped_to_max_chars(self):
        long_body = "A" * 500
        provider = self._make_provider([
            SearchResult(title="제목", body=long_body, url="https://example.com"),
        ])
        with _web_search_config(WEB_SEARCH_MAX_CHARS=200):
            result = search_web("[첨부 이미지 분석]\n오류", provider=provider)
        assert "A" * 200 in result
        assert "A" * 201 not in result


class TestSearXNGProvider:
    def _mock_response(self, data: dict, status_code: int = 200):
        mock = MagicMock()
        mock.status_code = status_code
        mock.json.return_value = data
        mock.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=mock)
            if status_code >= 400 else None
        )
        return mock

    def test_returns_results_on_success(self):
        payload = {
            "results": [
                {"title": "제목1", "content": "내용1", "url": "https://a.com"},
                {"title": "제목2", "content": "내용2", "url": "https://b.com"},
            ]
        }
        provider = SearXNGProvider("http://searxng:8080")
        with _patch("httpx.get", return_value=self._mock_response(payload)):
            results = provider.search("테스트", top_k=3, timeout=4.0)
        assert len(results) == 2
        assert results[0].title == "제목1"
        assert results[0].body == "내용1"
        assert results[0].url == "https://a.com"

    def test_respects_top_k(self):
        payload = {
            "results": [
                {"title": f"제목{i}", "content": f"내용{i}", "url": f"https://x{i}.com"}
                for i in range(5)
            ]
        }
        provider = SearXNGProvider("http://searxng:8080")
        with _patch("httpx.get", return_value=self._mock_response(payload)):
            results = provider.search("테스트", top_k=2, timeout=4.0)
        assert len(results) == 2

    def test_returns_empty_on_timeout(self):
        provider = SearXNGProvider("http://searxng:8080")
        with _patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            results = provider.search("테스트", top_k=3, timeout=4.0)
        assert results == []

    def test_returns_empty_on_connection_error(self):
        provider = SearXNGProvider("http://searxng:8080")
        with _patch("httpx.get", side_effect=Exception("connection refused")):
            results = provider.search("테스트", top_k=3, timeout=4.0)
        assert results == []

    def test_skips_result_without_content(self):
        payload = {
            "results": [
                {"title": "제목", "content": "", "url": "https://a.com"},
                {"title": "제목2", "content": "내용2", "url": "https://b.com"},
            ]
        }
        provider = SearXNGProvider("http://searxng:8080")
        with _patch("httpx.get", return_value=self._mock_response(payload)):
            results = provider.search("테스트", top_k=3, timeout=4.0)
        assert len(results) == 1
        assert results[0].title == "제목2"

