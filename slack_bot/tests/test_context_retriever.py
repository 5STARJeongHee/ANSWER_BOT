# context_retriever 서비스 단위 테스트 - RAG 검색 및 임베딩 로직
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock

# context_retriever는 db.repository를 임포트하는데, db.models가 SQLAlchemy 2.0+
# (DeclarativeBase)를 요구하지만 테스트 환경에는 1.x만 설치되어 있다.
# 이를 피하기 위해 db 서브패키지 전체를 sys.modules에 stub으로 등록한다.
_db_stub = MagicMock()
sys.modules.setdefault("db", _db_stub)
sys.modules.setdefault("db.models", _db_stub)
sys.modules.setdefault("db.repository", _db_stub)

# sentence_transformers도 테스트 환경에 없을 수 있으므로 stub 처리한다.
sys.modules.setdefault("sentence_transformers", MagicMock())

import pytest

import services.context_retriever as retriever_module
from services.context_retriever import (
    embed_text,
    _generate_rag_query,
    retrieve_context,
    format_context_for_prompt,
)


class TestEmbedText:
    def test_empty_string_returns_none(self):
        result = embed_text("")
        assert result is None

    def test_whitespace_only_returns_none(self):
        result = embed_text("   ")
        assert result is None

    def test_none_returns_none(self):
        result = embed_text(None)
        assert result is None

    def test_valid_text_returns_vector(self):
        mock_model = MagicMock()
        mock_model.encode.return_value = MagicMock(tolist=lambda: [0.1, 0.2, 0.3])

        with patch.object(retriever_module, "_get_embedding_model", return_value=mock_model):
            result = embed_text("안녕하세요")

        assert result == [0.1, 0.2, 0.3]
        mock_model.encode.assert_called_once_with("안녕하세요", normalize_embeddings=True)

    def test_embedding_model_exception_returns_none(self):
        mock_model = MagicMock()
        mock_model.encode.side_effect = RuntimeError("모델 로드 실패")

        with patch.object(retriever_module, "_get_embedding_model", return_value=mock_model):
            result = embed_text("텍스트")

        assert result is None


class TestGenerateRagQuery:
    def test_returns_llm_result_when_successful(self):
        with patch("services.context_retriever.call_rag_query", return_value="  회의실 예약  "):
            result = _generate_rag_query("회의실 어떻게 예약해?")
        assert result == "회의실 예약"

    def test_returns_original_question_when_llm_fails(self):
        with patch("services.context_retriever.call_rag_query", return_value=None):
            result = _generate_rag_query("원본 질문입니다")
        assert result == "원본 질문입니다"

    def test_returns_original_question_when_llm_returns_empty(self):
        with patch("services.context_retriever.call_rag_query", return_value="   "):
            result = _generate_rag_query("빈 응답일 때 원본 질문")
        assert result == "빈 응답일 때 원본 질문"


class TestRetrieveContext:
    def _make_session(self):
        return MagicMock()

    def test_returns_list_of_contexts(self):
        mock_session = self._make_session()
        expected = [
            {"chunk_text": "과거 대화 내용", "similarity": 0.9, "message_id": 1}
        ]

        with patch("services.context_retriever._generate_rag_query", return_value="검색 쿼리"), \
             patch.object(retriever_module, "embed_text", return_value=[0.1, 0.2]), \
             patch("services.context_retriever.search_similar_embeddings", return_value=expected):
            result = retrieve_context(mock_session, "테스트 질문")

        assert result == expected

    def test_returns_empty_list_on_search_exception(self):
        mock_session = self._make_session()

        with patch("services.context_retriever._generate_rag_query", return_value="쿼리"), \
             patch.object(retriever_module, "embed_text", return_value=[0.1]), \
             patch("services.context_retriever.search_similar_embeddings", side_effect=Exception("DB 오류")):
            result = retrieve_context(mock_session, "질문")

        assert result == []

    def test_passes_channel_id_to_search(self):
        mock_session = self._make_session()

        with patch("services.context_retriever._generate_rag_query", return_value="쿼리"), \
             patch.object(retriever_module, "embed_text", return_value=[0.1]), \
             patch("services.context_retriever.search_similar_embeddings", return_value=[]) as mock_search:
            retrieve_context(mock_session, "질문", channel_id="C_CHANNEL_123")

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["channel_id"] == "C_CHANNEL_123"

    def test_passes_top_k_to_search(self):
        mock_session = self._make_session()

        with patch("services.context_retriever._generate_rag_query", return_value="쿼리"), \
             patch.object(retriever_module, "embed_text", return_value=[0.1]), \
             patch("services.context_retriever.search_similar_embeddings", return_value=[]) as mock_search:
            retrieve_context(mock_session, "질문", top_k=3)

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["top_k"] == 3

    def test_embed_text_failure_passes_empty_list_to_search(self):
        """임베딩 실패 시 query_embedding=[] 로 검색을 진행한다."""
        mock_session = self._make_session()

        with patch("services.context_retriever._generate_rag_query", return_value="쿼리"), \
             patch.object(retriever_module, "embed_text", return_value=None), \
             patch("services.context_retriever.search_similar_embeddings", return_value=[]) as mock_search:
            retrieve_context(mock_session, "질문")

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["query_embedding"] == []


class TestFormatContextForPrompt:
    def test_empty_list_returns_placeholder(self):
        result = format_context_for_prompt([])
        assert result == "(관련 과거 대화 없음)"

    def test_single_context_formatted_correctly(self):
        contexts = [{"chunk_text": "회의실은 3층에 있습니다.", "similarity": 0.85, "message_id": 1}]
        result = format_context_for_prompt(contexts)
        assert "[1]" in result
        assert "0.85" in result
        assert "회의실은 3층에 있습니다." in result

    def test_multiple_contexts_all_included(self):
        contexts = [
            {"chunk_text": "내용 A", "similarity": 0.9, "message_id": 1},
            {"chunk_text": "내용 B", "similarity": 0.7, "message_id": 2},
        ]
        result = format_context_for_prompt(contexts)
        assert "[1]" in result
        assert "[2]" in result
        assert "내용 A" in result
        assert "내용 B" in result

    def test_context_with_empty_chunk_text_is_skipped(self):
        # enumerate는 전체 리스트 인덱스를 유지하므로, 첫 번째 항목이 빈 chunk여도
        # 두 번째 유효한 항목은 [2]로 출력된다. 비어있는 항목 자체만 제외된다.
        contexts = [
            {"chunk_text": "", "similarity": 0.9, "message_id": 1},
            {"chunk_text": "유효한 내용", "similarity": 0.8, "message_id": 2},
        ]
        result = format_context_for_prompt(contexts)
        assert "유효한 내용" in result
        # 빈 chunk_text 항목([1])은 출력에 포함되지 않는다
        assert "0.90" not in result
        # 유효한 항목은 두 번째 위치이므로 [2]로 출력된다
        assert "[2]" in result

    def test_all_empty_chunk_texts_returns_placeholder(self):
        contexts = [{"chunk_text": "   ", "similarity": 0.5, "message_id": 1}]
        result = format_context_for_prompt(contexts)
        assert result == "(관련 과거 대화 없음)"

    def test_missing_similarity_uses_zero(self):
        contexts = [{"chunk_text": "내용", "message_id": 1}]
        result = format_context_for_prompt(contexts)
        assert "0.00" in result
