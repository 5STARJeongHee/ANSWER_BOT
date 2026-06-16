# llm_service 단위 테스트 - OpenRouter API 호출 mock 기반
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock
import pytest

# openai 클라이언트는 모듈 임포트 시 생성되므로, 임포트 전에 mock 클라이언트를 주입한다.
import services.llm_service as llm_module
from services.llm_service import (
    _strip_json_fence,
    parse_json_response,
    call_with_fallback,
    call_classifier,
    call_qa,
    call_summary,
    call_rag_query,
)


class TestStripJsonFence:
    def test_removes_json_fence(self):
        raw = "```json\n{\"key\": \"value\"}\n```"
        result = _strip_json_fence(raw)
        assert result == '{"key": "value"}'

    def test_removes_plain_code_fence(self):
        raw = "```\n{\"a\": 1}\n```"
        result = _strip_json_fence(raw)
        assert result == '{"a": 1}'

    def test_no_fence_returns_stripped(self):
        raw = '  {"a": 1}  '
        result = _strip_json_fence(raw)
        assert result == '{"a": 1}'

    def test_empty_string_returns_empty(self):
        result = _strip_json_fence("")
        assert result == ""


class TestParseJsonResponse:
    def test_parses_valid_json(self):
        raw = '{"category": "QUESTION", "confidence": 0.9}'
        default = {}
        result = parse_json_response(raw, default)
        assert result["category"] == "QUESTION"
        assert result["confidence"] == 0.9

    def test_returns_default_on_empty_string(self):
        default = {"error": True}
        result = parse_json_response("", default)
        assert result == {"error": True}

    def test_returns_default_on_invalid_json(self):
        default = {"fallback": "yes"}
        result = parse_json_response("not-json-at-all!!!", default)
        assert result == {"fallback": "yes"}

    def test_parses_json_with_markdown_fence(self):
        raw = "```json\n{\"ok\": true}\n```"
        result = parse_json_response(raw, {})
        assert result == {"ok": True}

    def test_returns_default_on_incomplete_json(self):
        default = {"d": 1}
        result = parse_json_response('{"key":', default)
        assert result == {"d": 1}


class TestCallWithRetry:
    """_call_with_retry는 내부 함수이므로 call_with_fallback을 통해 간접 테스트한다."""

    def _make_mock_response(self, content: str):
        mock_resp = MagicMock()
        mock_resp.choices[0].message.content = content
        return mock_resp

    def test_success_on_first_attempt(self):
        mock_resp = self._make_mock_response("안녕하세요")
        with patch.object(llm_module._client.chat.completions, "create", return_value=mock_resp):
            result = call_with_fallback(["model-a"], [{"role": "user", "content": "hi"}], 50)
        assert result == "안녕하세요"

    def test_returns_none_when_all_models_fail(self):
        with patch.object(llm_module._client.chat.completions, "create", side_effect=Exception("api down")):
            result = call_with_fallback(
                ["model-a", "model-b"],
                [{"role": "user", "content": "hi"}],
                50,
            )
        assert result is None

    def test_falls_back_to_second_model_on_first_failure(self):
        call_count = {"n": 0}

        def side_effect(**kwargs):
            call_count["n"] += 1
            if kwargs["model"] == "model-a":
                raise Exception("model-a down")
            mock_resp = self._make_mock_response("fallback 응답")
            return mock_resp

        with patch.object(llm_module._client.chat.completions, "create", side_effect=side_effect):
            result = call_with_fallback(
                ["model-a", "model-b"],
                [{"role": "user", "content": "질문"}],
                50,
            )
        assert result == "fallback 응답"

    def test_rate_limit_retries_with_sleep(self):
        from openai import RateLimitError

        responses = [
            RateLimitError("rate limit", response=MagicMock(status_code=429), body={}),
            self._make_mock_response("재시도 성공"),
        ]
        responses_iter = iter(responses)

        def side_effect(**kwargs):
            val = next(responses_iter)
            if isinstance(val, Exception):
                raise val
            return val

        with patch.object(llm_module._client.chat.completions, "create", side_effect=side_effect), \
             patch("services.llm_service.time.sleep"):
            result = call_with_fallback(["model-a"], [{"role": "user", "content": "hi"}], 50)
        assert result == "재시도 성공"

    def test_content_none_returns_none_for_that_model(self):
        """응답 content가 None인 경우 (reasoning-only 모드) None을 반환한다."""
        mock_resp = self._make_mock_response(None)
        with patch.object(llm_module._client.chat.completions, "create", return_value=mock_resp):
            result = call_with_fallback(["model-reasoning"], [{"role": "user", "content": "x"}], 50)
        assert result is None

    def test_api_timeout_retries(self):
        from openai import APITimeoutError

        responses = [
            APITimeoutError(request=MagicMock()),
            self._make_mock_response("timeout 후 성공"),
        ]
        responses_iter = iter(responses)

        def side_effect(**kwargs):
            val = next(responses_iter)
            if isinstance(val, Exception):
                raise val
            return val

        with patch.object(llm_module._client.chat.completions, "create", side_effect=side_effect), \
             patch("services.llm_service.time.sleep"):
            result = call_with_fallback(["model-a"], [{"role": "user", "content": "hi"}], 50)
        assert result == "timeout 후 성공"


class TestCallClassifier:
    def test_call_classifier_uses_json_response_format(self):
        """call_classifier는 response_format=json_object를 사용해야 한다."""
        with patch("services.llm_service.call_with_fallback") as mock_fallback:
            mock_fallback.return_value = '{"category":"QUESTION"}'
            call_classifier([{"role": "user", "content": "test"}])
            _, kwargs = mock_fallback.call_args
            assert kwargs.get("response_format") == {"type": "json_object"}

    def test_call_classifier_passes_classifier_chain(self):
        import config
        with patch("services.llm_service.call_with_fallback") as mock_fallback:
            mock_fallback.return_value = "{}"
            call_classifier([{"role": "user", "content": "test"}])
            # call_with_fallback은 키워드 인자로 호출되므로 call_args.kwargs를 확인한다
            call_args = mock_fallback.call_args
            # Python 3.8에서는 call_args[1]이 kwargs이다
            kwargs = call_args[1]
            assert kwargs.get("model_chain") == config.CLASSIFIER_FALLBACK_CHAIN


class TestCallQa:
    def test_call_qa_returns_response(self):
        with patch("services.llm_service.call_with_fallback", return_value="QA 답변") as mock_fb:
            result = call_qa([{"role": "user", "content": "질문"}])
        assert result == "QA 답변"

    def test_call_qa_passes_qa_chain(self):
        import config
        with patch("services.llm_service.call_with_fallback") as mock_fallback:
            mock_fallback.return_value = "답변"
            call_qa([{"role": "user", "content": "test"}])
            call_args = mock_fallback.call_args
            kwargs = call_args[1]
            assert kwargs.get("model_chain") == config.QA_FALLBACK_CHAIN


class TestCallSummary:
    def test_call_summary_returns_response(self):
        with patch("services.llm_service.call_with_fallback", return_value="요약 결과"):
            result = call_summary([{"role": "user", "content": "요약 대상"}])
        assert result == "요약 결과"


class TestCallRagQuery:
    def test_call_rag_query_returns_response(self):
        with patch("services.llm_service.call_with_fallback", return_value="검색 쿼리"):
            result = call_rag_query([{"role": "user", "content": "원본 질문"}])
        assert result == "검색 쿼리"
