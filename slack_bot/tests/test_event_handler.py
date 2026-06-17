# event_handler 단위 테스트 - Bolt 클로저 제외, 순수 헬퍼 함수 테스트
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock, call

# event_handler는 slack_bolt, slack_sdk, db.models 등에 의존하는데
# 테스트 환경에 설치되어 있지 않다. sys.modules에 stub을 먼저 등록해야 한다.
_slack_bolt_stub = MagicMock()
sys.modules.setdefault("slack_bolt", _slack_bolt_stub)
sys.modules.setdefault("slack_bolt.App", _slack_bolt_stub)

_slack_sdk_stub = MagicMock()
sys.modules.setdefault("slack_sdk", _slack_sdk_stub)
sys.modules.setdefault("slack_sdk.errors", _slack_sdk_stub)

_db_stub = MagicMock()
sys.modules.setdefault("db", _db_stub)
sys.modules.setdefault("db.models", _db_stub)
sys.modules.setdefault("db.repository", _db_stub)

# sentence_transformers stub (context_retriever 경유)
sys.modules.setdefault("sentence_transformers", MagicMock())

# ui 서브패키지 stub
_ui_stub = MagicMock()
sys.modules.setdefault("ui", _ui_stub)
sys.modules.setdefault("ui.message_blocks", _ui_stub)
sys.modules.setdefault("ui.reaction_handler", _ui_stub)

# PIL / image_processor stub (로컬 환경 Pillow DLL 호환성 문제 우회)
_pil_stub = MagicMock()
sys.modules.setdefault("PIL", _pil_stub)
sys.modules.setdefault("PIL.Image", _pil_stub)

import pytest

# Bolt App 임포트를 피하기 위해 handlers.event_handler의 헬퍼들을 직접 임포트한다.
# register_handlers는 App 인스턴스를 요구하므로 테스트 대상에서 제외한다.
from handlers.event_handler import (
    _clean_mention_text,
    _evaluate_answer,
    _save_message_and_embed,
    _send_error_or_fallback,
    _delete_thinking_msg,
)


class TestCleanMentionText:
    def test_removes_mention_tag(self):
        result = _clean_mention_text("<@U_BOT> 안녕하세요", "U_BOT")
        assert result == "안녕하세요"

    def test_removes_mention_from_middle(self):
        result = _clean_mention_text("안녕 <@U_BOT> 도와줘", "U_BOT")
        assert result == "안녕  도와줘"

    def test_no_mention_returns_stripped(self):
        result = _clean_mention_text("  일반 메시지  ", "U_BOT")
        assert result == "일반 메시지"

    def test_empty_string_returns_empty(self):
        result = _clean_mention_text("", "U_BOT")
        assert result == ""

    def test_only_mention_returns_empty(self):
        result = _clean_mention_text("<@U_BOT>", "U_BOT")
        assert result == ""

    def test_different_bot_id_not_removed(self):
        result = _clean_mention_text("<@U_OTHER> 메시지", "U_BOT")
        assert "<@U_OTHER>" in result


class TestEvaluateAnswer:
    def test_returns_true_when_no_fallback_keyword(self):
        result = _evaluate_answer("질문", "네, 연차는 HR 시스템에서 신청하면 됩니다. [AI 생성 답변]")
        assert result is True

    def test_returns_false_when_contains_confirmation_needed(self):
        result = _evaluate_answer("정책 질문", "해당 내용은 확인이 필요합니다. [AI 생성 답변]")
        assert result is False

    def test_returns_false_when_contains_manager_contact(self):
        result = _evaluate_answer("질문", "담당자에게 문의 부탁드립니다.")
        assert result is False

    def test_returns_false_when_contains_unknown_keyword(self):
        result = _evaluate_answer("질문", "정확한 내용은 알 수 없습니다.")
        assert result is False

    def test_returns_true_on_empty_answer(self):
        result = _evaluate_answer("질문", "")
        assert result is True


class TestSaveMessageAndEmbed:
    def _make_session_factory(self, msg_id=42, duplicate=False):
        """session_factory mock을 생성하는 헬퍼."""
        mock_session = MagicMock()
        if duplicate:
            # upsert_message가 None을 반환하는 경우 (중복)
            mock_upsert_return = None
        else:
            mock_upsert_return = MagicMock(id=msg_id)

        def mock_factory():
            return mock_session

        return mock_factory, mock_session, mock_upsert_return

    def test_returns_message_id_on_success(self):
        mock_factory, mock_session, _ = self._make_session_factory()

        with patch("handlers.event_handler.upsert_message") as mock_upsert, \
             patch("handlers.event_handler.embed_text", return_value=[0.1, 0.2]), \
             patch("db.repository.save_embedding"):
            mock_upsert.return_value = MagicMock(id=42)
            result = _save_message_and_embed(
                session_factory=mock_factory,
                event_id="EVT_001",
                channel_id="C_TEST",
                thread_ts="123.456",
                message_ts="789.000",
                user_id="U_USER",
                role="user",
                content="테스트 메시지",
            )

        assert result == 42

    def test_returns_none_on_duplicate_message(self):
        """upsert_message가 None 반환 시 (중복), _save_message_and_embed도 None을 반환해야 한다."""
        mock_factory, mock_session, _ = self._make_session_factory()

        with patch("handlers.event_handler.upsert_message", return_value=None), \
             patch("handlers.event_handler.embed_text", return_value=None):
            result = _save_message_and_embed(
                session_factory=mock_factory,
                event_id="DUPLICATE",
                channel_id="C_TEST",
                thread_ts=None,
                message_ts="001",
                user_id=None,
                role="bot",
                content="중복 메시지",
            )

        assert result is None

    def test_returns_none_on_upsert_exception(self):
        """upsert_message 예외 발생 시 None을 반환하고 rollback이 호출되어야 한다."""
        mock_factory, mock_session, _ = self._make_session_factory()

        with patch("handlers.event_handler.upsert_message", side_effect=Exception("DB 연결 실패")):
            result = _save_message_and_embed(
                session_factory=mock_factory,
                event_id=None,
                channel_id="C_TEST",
                thread_ts=None,
                message_ts="002",
                user_id=None,
                role="user",
                content="오류 메시지",
            )

        assert result is None
        mock_session.rollback.assert_called_once()

    def test_pii_content_is_filtered_before_save(self):
        """PII가 포함된 메시지는 마스킹 후 저장되어야 한다."""
        mock_factory, mock_session, _ = self._make_session_factory()

        with patch("handlers.event_handler.upsert_message") as mock_upsert, \
             patch("handlers.event_handler.embed_text", return_value=None), \
             patch("db.repository.save_embedding"):
            mock_upsert.return_value = MagicMock(id=99)
            _save_message_and_embed(
                session_factory=mock_factory,
                event_id=None,
                channel_id="C_PII",
                thread_ts=None,
                message_ts="003",
                user_id=None,
                role="user",
                content="제 이메일은 user@example.com 입니다",
            )

        # upsert_message에 전달된 content에 원본 이메일이 없어야 한다
        call_kwargs = mock_upsert.call_args[1]
        assert "user@example.com" not in call_kwargs["content"]
        assert "[EMAIL_REMOVED]" in call_kwargs["content"]

    def test_embedding_failure_does_not_fail_message_save(self):
        """임베딩 저장 실패는 전체 저장 실패로 이어지지 않는다."""
        mock_factory, mock_session, _ = self._make_session_factory()

        with patch("handlers.event_handler.upsert_message") as mock_upsert, \
             patch("handlers.event_handler.embed_text", return_value=[0.1]), \
             patch("db.repository.save_embedding", side_effect=Exception("pgvector 오류")):
            mock_upsert.return_value = MagicMock(id=55)
            result = _save_message_and_embed(
                session_factory=mock_factory,
                event_id=None,
                channel_id="C_TEST",
                thread_ts=None,
                message_ts="004",
                user_id=None,
                role="bot",
                content="임베딩 실패 메시지",
            )

        # 임베딩 실패에도 message_id가 반환되어야 한다
        assert result == 55


class TestDeleteThinkingMsg:
    def test_deletes_message_when_ts_provided(self):
        mock_client = MagicMock()
        _delete_thinking_msg(mock_client, "C_CHANNEL", "123.456")
        mock_client.chat_delete.assert_called_once_with(channel="C_CHANNEL", ts="123.456")

    def test_does_nothing_when_ts_is_none(self):
        mock_client = MagicMock()
        _delete_thinking_msg(mock_client, "C_CHANNEL", None)
        mock_client.chat_delete.assert_not_called()

    def test_swallows_exception_on_delete_failure(self):
        """삭제 실패 시 예외를 전파하지 않아야 한다."""
        mock_client = MagicMock()
        mock_client.chat_delete.side_effect = Exception("메시지 없음")
        # 예외 없이 실행되어야 한다
        _delete_thinking_msg(mock_client, "C_CHANNEL", "999.999")


class TestSendErrorOrFallback:
    def test_calls_post_error(self):
        mock_client = MagicMock()

        with patch("handlers.event_handler.post_error") as mock_post_error:
            _send_error_or_fallback(
                client=mock_client,
                channel_id="C_CHANNEL",
                thread_ts="ts",
                question="질문",
                thinking_ts="think_ts",
            )

        mock_post_error.assert_called_once_with(
            client=mock_client,
            channel="C_CHANNEL",
            thread_ts="ts",
            thinking_ts="think_ts",
        )
