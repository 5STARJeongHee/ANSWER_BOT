# 토큰 카운터 단위 테스트
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from utils.token_counter import estimate_tokens, estimate_message_tokens, trim_messages_to_budget


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_korean_text(self):
        # 한글은 2자당 약 1토큰
        text = "안녕하세요 반갑습니다"
        result = estimate_tokens(text)
        assert result > 0

    def test_english_text(self):
        text = "hello world this is a test"
        result = estimate_tokens(text)
        assert result > 0

    def test_mixed_text(self):
        text = "Hello 안녕하세요 world"
        result = estimate_tokens(text)
        assert result > 0

    def test_longer_text_has_more_tokens(self):
        short = "안녕"
        long_text = "안녕하세요 반갑습니다 오늘 날씨가 좋네요 회의실 예약은 어떻게 하나요?"
        assert estimate_tokens(long_text) > estimate_tokens(short)


class TestEstimateMessageTokens:
    def test_empty_list(self):
        assert estimate_message_tokens([]) == 3  # 프라이밍 오버헤드

    def test_single_message(self):
        messages = [{"role": "user", "content": "안녕하세요"}]
        result = estimate_message_tokens(messages)
        assert result > 0

    def test_multiple_messages(self):
        messages = [
            {"role": "user", "content": "질문입니다"},
            {"role": "assistant", "content": "답변입니다"},
        ]
        result = estimate_message_tokens(messages)
        single = estimate_message_tokens([messages[0]])
        assert result > single


class TestTrimMessagesToBudget:
    def test_empty_returns_empty(self):
        result = trim_messages_to_budget([], "system", 1000)
        assert result == []

    def test_within_budget_returns_all(self):
        messages = [
            {"role": "user", "content": "짧은 질문"},
            {"role": "assistant", "content": "짧은 답변"},
        ]
        result = trim_messages_to_budget(messages, "시스템 프롬프트", 6000)
        assert len(result) == 2

    def test_over_budget_trims_oldest(self):
        # 매우 작은 예산으로 트리밍 발생 유도
        messages = [
            {"role": "user", "content": "오래된 메시지 1"},
            {"role": "user", "content": "오래된 메시지 2"},
            {"role": "user", "content": "최신 질문"},
        ]
        result = trim_messages_to_budget(messages, "시스템", 30, keep_last_n=1)
        # 최소 마지막 1개는 보존
        assert len(result) >= 1
        assert result[-1]["content"] == "최신 질문"

    def test_keep_last_n_always_preserved(self):
        messages = [{"role": "user", "content": f"메시지 {i}"} for i in range(10)]
        result = trim_messages_to_budget(messages, "system", 1, keep_last_n=2)
        # keep_last_n=2 이므로 최소 2개 보존
        assert len(result) >= 2
        assert result[-1]["content"] == "메시지 9"
        assert result[-2]["content"] == "메시지 8"
