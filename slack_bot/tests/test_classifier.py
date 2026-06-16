# classifier 서비스 단위 테스트
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock
import pytest

import services.classifier as classifier_module
from services.classifier import (
    classify_message,
    MessageCategory,
    ClassifyResult,
    _is_bot_message_by_heuristic,
    _CACHE_MAX_SIZE,
)


class TestMessageCategory:
    def test_enum_values_exist(self):
        assert MessageCategory.QUESTION == "QUESTION"
        assert MessageCategory.REQUEST == "REQUEST"
        assert MessageCategory.NONE == "NONE"


class TestClassifyResultNoneResult:
    def test_none_result_returns_correct_defaults(self):
        result = ClassifyResult.none_result()
        assert result.category == MessageCategory.NONE
        assert result.confidence == 1.0
        assert result.is_actionable is False

    def test_none_result_is_actionable_false(self):
        result = ClassifyResult.none_result()
        assert not result.is_actionable


class TestIsBotMessageByHeuristic:
    def test_returns_true_for_qa_bot_prefix(self):
        assert _is_bot_message_by_heuristic("안녕하세요! 사내 Q&A 봇입니다 도움이 필요하신가요?")

    def test_returns_true_for_ai_answer_prefix(self):
        assert _is_bot_message_by_heuristic("[AI 생성 답변] 이것이 답변입니다.")

    def test_returns_true_for_thinking_prefix(self):
        assert _is_bot_message_by_heuristic("답변을 생성 중입니다...")

    def test_returns_false_for_normal_user_message(self):
        assert not _is_bot_message_by_heuristic("오늘 회의 몇 시에 해요?")

    def test_returns_false_for_empty_string(self):
        assert not _is_bot_message_by_heuristic("")


class TestClassifyMessageEmptyInputs:
    def test_empty_string_returns_none_category(self):
        result = classify_message("")
        assert result.category == MessageCategory.NONE
        assert result.is_actionable is False

    def test_whitespace_only_returns_none_category(self):
        result = classify_message("   ")
        assert result.category == MessageCategory.NONE

    def test_none_message_returns_none_category(self):
        # None을 넘기면 not message 조건에 걸려 NONE 반환
        result = classify_message(None)
        assert result.category == MessageCategory.NONE


class TestClassifyMessageBotFiltering:
    def test_same_user_and_bot_id_returns_none(self):
        result = classify_message(
            "어떤 메시지",
            bot_user_id="U_BOT",
            sender_user_id="U_BOT",
        )
        assert result.category == MessageCategory.NONE

    def test_different_user_does_not_short_circuit(self):
        # LLM 호출이 일어나야 하므로 mock 필요
        with patch("services.classifier.call_classifier") as mock_call, \
             patch("services.classifier.parse_json_response") as mock_parse:
            mock_call.return_value = '{"category":"QUESTION","confidence":0.9,"reason":"test"}'
            mock_parse.return_value = {"category": "QUESTION", "confidence": 0.9, "reason": "test"}
            result = classify_message(
                "회의실 예약 어떻게 해요?",
                bot_user_id="U_BOT",
                sender_user_id="U_USER",
            )
        assert result.category == MessageCategory.QUESTION

    def test_bot_heuristic_prefix_returns_none(self):
        result = classify_message("[AI 생성 답변] 도움이 되셨으면 합니다.")
        assert result.category == MessageCategory.NONE


class TestClassifyMessageMention:
    def test_is_mention_returns_question_immediately(self):
        # is_mention=True이면 LLM 호출 없이 즉시 QUESTION 반환
        result = classify_message(
            "회의실 예약 방법 알려줘",
            is_mention=True,
        )
        assert result.category == MessageCategory.QUESTION
        assert result.confidence == 1.0
        assert result.is_actionable is True
        assert result.reason == "앱 멘션 이벤트"

    def test_is_mention_does_not_call_llm(self):
        with patch("services.classifier.call_classifier") as mock_call:
            classify_message("뭔가 물어볼게", is_mention=True)
            mock_call.assert_not_called()


class TestClassifyMessageLlmIntegration:
    def _mock_classify(self, category: str, confidence: float = 0.9, reason: str = "test"):
        """LLM 분류기를 mock하고 classify_message를 호출하는 헬퍼."""
        with patch("services.classifier.call_classifier") as mock_call, \
             patch("services.classifier.parse_json_response") as mock_parse:
            mock_call.return_value = f'{{"category":"{category}","confidence":{confidence},"reason":"{reason}"}}'
            mock_parse.return_value = {
                "category": category,
                "confidence": confidence,
                "reason": reason,
            }
            return classify_message("테스트 메시지 유니크1234")

    def test_question_category_is_actionable(self):
        result = self._mock_classify("QUESTION")
        assert result.category == MessageCategory.QUESTION
        assert result.is_actionable is True

    def test_request_category_is_actionable(self):
        result = self._mock_classify("REQUEST")
        assert result.category == MessageCategory.REQUEST
        assert result.is_actionable is True

    def test_none_category_is_not_actionable(self):
        result = self._mock_classify("NONE")
        assert result.category == MessageCategory.NONE
        assert result.is_actionable is False

    def test_invalid_category_falls_back_to_none(self):
        with patch("services.classifier.call_classifier") as mock_call, \
             patch("services.classifier.parse_json_response") as mock_parse:
            mock_call.return_value = '{"category":"INVALID","confidence":0.5,"reason":"bad"}'
            mock_parse.return_value = {"category": "INVALID", "confidence": 0.5, "reason": "bad"}
            result = classify_message("알 수 없는 카테고리 테스트 메시지abc")
        assert result.category == MessageCategory.NONE

    def test_llm_returns_none_uses_default(self):
        with patch("services.classifier.call_classifier", return_value=None), \
             patch("services.classifier.parse_json_response") as mock_parse:
            mock_parse.return_value = {"category": "NONE", "confidence": 0.0, "reason": "분류 실패"}
            result = classify_message("LLM 실패 케이스 유니크xyz987")
        assert result.category == MessageCategory.NONE

    def test_result_is_cached_on_second_call(self):
        msg = "캐시 테스트용 고유 메시지 abc999"
        with patch("services.classifier.call_classifier") as mock_call, \
             patch("services.classifier.parse_json_response") as mock_parse:
            mock_call.return_value = '{"category":"QUESTION","confidence":0.8,"reason":"ok"}'
            mock_parse.return_value = {"category": "QUESTION", "confidence": 0.8, "reason": "ok"}
            classify_message(msg)
            classify_message(msg)
            # 두 번 호출했어도 LLM은 1번만 호출되어야 한다 (캐시 히트)
            assert mock_call.call_count == 1


class TestClassifyMessageCacheEviction:
    def test_cache_does_not_exceed_max_size(self):
        """캐시가 최대 크기를 초과하면 가장 오래된 항목이 제거된다."""
        classifier_module._classify_cache.clear()

        with patch("services.classifier.call_classifier") as mock_call, \
             patch("services.classifier.parse_json_response") as mock_parse:
            mock_call.return_value = "{}"
            mock_parse.return_value = {"category": "NONE", "confidence": 0.5, "reason": ""}

            for i in range(_CACHE_MAX_SIZE + 5):
                classify_message(f"캐시 가득 채우기 테스트 메시지 번호 {i:05d}")

        assert len(classifier_module._classify_cache) <= _CACHE_MAX_SIZE
