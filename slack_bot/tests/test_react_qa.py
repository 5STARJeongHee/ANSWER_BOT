# ReAct QA 엔진 단위 테스트 — 루프 동작 및 액션 파싱 검증
import sys
from unittest.mock import MagicMock, patch

# 의존 모듈 stub 등록
_config_stub = MagicMock()
_config_stub.ENABLE_WEB_SEARCH = False
_config_stub.MAX_CONTEXT_TOKENS = 6000
_config_stub.MAX_CONCURRENT_VISION = 1
_config_stub.RAG_TOP_K = 3
_config_stub.ENABLE_RERANKING = False
_config_stub.ENABLE_HYBRID_SEARCH = False
_config_stub.ENABLE_VECTOR_SEARCH = False
_config_stub.RAG_RERANK_POOL_K = 15
_config_stub.RAG_SIMILARITY_THRESHOLD = 0.55
_config_stub.RAG_IMAGE_SIMILARITY_THRESHOLD = 0.45
# setdefault 사용: 다른 파일이 먼저 실제 config를 등록했어도 오염하지 않는다.
sys.modules.setdefault("config", _config_stub)

_retriever_stub = MagicMock()
_retriever_stub.retrieve_context = MagicMock(return_value=[])
_retriever_stub.format_context_for_prompt = MagicMock(return_value="")
sys.modules["services.context_retriever"] = _retriever_stub

_web_stub = MagicMock()
_web_stub.search_web = MagicMock(return_value="")
_web_stub.format_web_search_for_prompt = MagicMock(return_value="")
sys.modules["services.web_search"] = _web_stub

_llm_stub = MagicMock()
sys.modules["services.llm_service"] = _llm_stub

_token_stub = MagicMock()
_token_stub.trim_messages_to_budget = MagicMock(side_effect=lambda msgs, *a, **kw: msgs)
sys.modules.setdefault("utils.token_counter", _token_stub)

import pytest
from services.react_qa import react_qa, _parse_action, ReactResult
import services.react_qa as _react_qa_mod


class TestParseAction:
    def test_answer_tag(self):
        action, value = _parse_action("<answer>최종 답변입니다.</answer>")
        assert action == "answer"
        assert value == "최종 답변입니다."

    def test_search_rag_tag(self):
        action, value = _parse_action("<search_rag>Redis 연결 오류</search_rag>")
        assert action == "search_rag"
        assert value == "Redis 연결 오류"

    def test_search_web_tag(self):
        action, value = _parse_action("<search_web>최신 Python 버전</search_web>")
        assert action == "search_web"
        assert value == "최신 Python 버전"

    def test_no_tag_treated_as_answer(self):
        action, value = _parse_action("태그 없는 일반 텍스트 응답")
        assert action == "answer"
        assert value == "태그 없는 일반 텍스트 응답"

    def test_case_insensitive(self):
        action, value = _parse_action("<ANSWER>대문자 태그</ANSWER>")
        assert action == "answer"
        assert value == "대문자 태그"

    def test_multiline_answer(self):
        action, value = _parse_action("<answer>첫 줄\n둘째 줄</answer>")
        assert action == "answer"
        assert "첫 줄" in value


class TestReactQaLoop:
    def setup_method(self):
        # test_web_search.py가 services.web_search를 실제 모듈로 교체할 수 있으므로 복원한다.
        sys.modules["services.web_search"] = _web_stub
        sys.modules["services.llm_service"] = _llm_stub
        sys.modules["services.context_retriever"] = _retriever_stub
        # react_qa 모듈의 config 변수를 _config_stub으로 주입 (전역 sys.modules 오염 없이)
        _react_qa_mod.config = _config_stub

    def _run(self, responses, max_iter=3, enable_web=False):
        _config_stub.ENABLE_WEB_SEARCH = enable_web
        _llm_stub.call_qa = MagicMock(side_effect=responses)
        _retriever_stub.retrieve_context.reset_mock()
        _retriever_stub.retrieve_context.side_effect = None
        _retriever_stub.retrieve_context.return_value = []
        _retriever_stub.format_context_for_prompt.return_value = ""
        session = MagicMock()
        return react_qa(session, "테스트 질문", "C123", max_iterations=max_iter)

    def test_immediate_answer(self):
        result = self._run(["<answer>즉시 답변</answer>"])
        assert result.answer == "즉시 답변"
        assert result.iterations == 1
        assert result.used_web_search is False

    def test_rag_then_answer(self):
        ctx = [{"chunk_text": "관련 내용", "similarity": 0.8}]
        _retriever_stub.retrieve_context.side_effect = [[], ctx]
        _retriever_stub.format_context_for_prompt.return_value = "관련 내용"
        _llm_stub.call_qa = MagicMock(side_effect=[
            "<search_rag>추가 검색 쿼리</search_rag>",
            "<answer>RAG 후 답변</answer>",
        ])
        session = MagicMock()
        result = react_qa(session, "질문", "C123", max_iterations=3)
        assert result.answer == "RAG 후 답변"
        assert result.iterations == 2
        assert len(result.rag_contexts) >= 1

    def test_web_search_then_answer(self):
        _config_stub.ENABLE_WEB_SEARCH = True
        _retriever_stub.retrieve_context.side_effect = None
        _retriever_stub.retrieve_context.return_value = []
        _web_stub.search_web.return_value = "검색 결과"
        _web_stub.format_web_search_for_prompt.return_value = "[웹 결과]"
        _llm_stub.call_qa = MagicMock(side_effect=[
            "<search_web>최신 정보 검색</search_web>",
            "<answer>웹 후 답변</answer>",
        ])
        session = MagicMock()
        result = react_qa(session, "질문", "C123", max_iterations=3)
        assert result.answer == "웹 후 답변"
        assert result.used_web_search is True
        _config_stub.ENABLE_WEB_SEARCH = False

    def test_max_iterations_forced(self):
        result = self._run(
            ["<search_rag>q1</search_rag>",
             "<search_rag>q2</search_rag>",
             "<answer>강제 답변</answer>"],
            max_iter=3,
        )
        assert result.answer == "강제 답변"
        assert result.iterations == 3

    def test_call_qa_returns_none_no_crash(self):
        result = self._run([None])
        assert result.answer == ""
        assert result.iterations == 3  # max_iterations 도달

    def test_no_tag_treated_as_answer(self):
        result = self._run(["일반 텍스트 응답입니다."])
        assert result.answer == "일반 텍스트 응답입니다."
        assert result.iterations == 1
