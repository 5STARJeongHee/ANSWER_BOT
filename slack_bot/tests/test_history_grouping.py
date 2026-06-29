# 히스토리 그룹핑 기능 — repository/block builder 단위 테스트
import importlib.util
import os
import sys
from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock

import pytest

# test_event_handler.py가 ui.message_blocks를 MagicMock으로 stub한다.
# sys.modules를 우회하여 실제 모듈을 직접 로드한다.
_mb_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui", "message_blocks.py")
_mb_spec = importlib.util.spec_from_file_location("_real_message_blocks", _mb_path)
_mb_module = importlib.util.module_from_spec(_mb_spec)
_mb_spec.loader.exec_module(_mb_module)
build_history_grouped_blocks = _mb_module.build_history_grouped_blocks

# db.models stub (SQLAlchemy 2.0 DeclarativeBase 없이 db.repository 임포트 허용)
# test_event_handler.py, test_context_retriever.py가 db.repository도 stub하므로
# importlib로 실제 모듈을 직접 로드한다.
_db_models_stub = MagicMock()
sys.modules["db.models"] = _db_models_stub

_repo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "repository.py")
_repo_spec = importlib.util.spec_from_file_location("_real_repository", _repo_path)
_repo_module = importlib.util.module_from_spec(_repo_spec)
_repo_spec.loader.exec_module(_repo_module)
_group_messages_by_topic = _repo_module._group_messages_by_topic


# ---------------------------------------------------------------------------
# build_history_grouped_blocks 테스트
# ---------------------------------------------------------------------------

def _make_entry(day: str, user_id: str, q: str, a: Optional[str] = None) -> dict:
    return {
        "q_preview": q,
        "a_preview": a,
        "created_at": datetime.strptime(f"2026.{day}", "%Y.%m.%d"),
        "user_id": user_id,
    }


class TestBuildHistoryGroupedBlocks:
    def test_empty_returns_no_data_message(self):
        payload = build_history_grouped_blocks([], 0, "테스트채널", 7)
        texts = [
            b["text"]["text"]
            for b in payload["blocks"]
            if b.get("type") == "section"
        ]
        assert any("없습니다" in t for t in texts)

    def test_header_contains_channel_name(self):
        payload = build_history_grouped_blocks([], 0, "#general", 7)
        header_block = next(b for b in payload["blocks"] if b["type"] == "header")
        assert "#general" in header_block["text"]["text"]

    def test_single_topic_renders_correctly(self):
        groups = [
            {
                "topic": "Redis 오류",
                "count": 2,
                "entries": [
                    _make_entry("06.25", "U111", "Redis 연결이 안됩니다", "설정 확인하세요"),
                    _make_entry("06.26", "U222", "Redis 타임아웃 발생"),
                ],
            }
        ]
        payload = build_history_grouped_blocks(groups, 2, "#dev", 7)
        block_texts = " ".join(str(b) for b in payload["blocks"])
        assert "Redis 오류" in block_texts
        assert "U111" in block_texts
        assert "Redis 연결이 안됩니다" in block_texts

    def test_overflow_indicator_shown(self):
        groups = [
            {
                "topic": "DB 이슈",
                "count": 5,
                "entries": [
                    _make_entry("06.20", "U100", f"질문{i}")
                    for i in range(3)
                ],
            }
        ]
        payload = build_history_grouped_blocks(groups, 5, "#dev", 7)
        context_texts = [
            e["text"]
            for b in payload["blocks"]
            if b["type"] == "context"
            for e in b["elements"]
        ]
        overflow_texts = [t for t in context_texts if "더 있음" in t]
        assert overflow_texts, "overflow 문구가 렌더링되어야 한다"
        assert "+2건" in overflow_texts[0]

    def test_footer_contains_total_count(self):
        groups = [
            {"topic": "배포", "count": 3, "entries": [_make_entry("06.01", "U1", "Q1")]},
        ]
        payload = build_history_grouped_blocks(groups, 3, "#ops", 14)
        footer_block = payload["blocks"][-1]
        footer_text = footer_block["elements"][0]["text"]
        assert "3건" in footer_text
        assert "14일" in footer_text

    def test_all_unclassified_shows_normalization_hint(self):
        groups = [
            {"topic": "미분류", "count": 2, "entries": [_make_entry("06.15", "U9", "질문")]},
        ]
        payload = build_history_grouped_blocks(groups, 2, "#ch", 7)
        block_texts = " ".join(str(b) for b in payload["blocks"])
        assert "정규화 실행" in block_texts

    def test_classified_topics_do_not_show_normalization_hint(self):
        groups = [
            {"topic": "Kubernetes", "count": 1, "entries": [_make_entry("06.10", "U5", "Pod 재시작")]},
        ]
        payload = build_history_grouped_blocks(groups, 1, "#ch", 7)
        block_texts = " ".join(str(b) for b in payload["blocks"])
        assert "정규화 실행" not in block_texts

    def test_block_count_within_slack_limit(self):
        groups = [
            {
                "topic": f"주제{i}",
                "count": 3,
                "entries": [_make_entry("06.01", "U1", f"질문{i}")],
            }
            for i in range(10)
        ]
        payload = build_history_grouped_blocks(groups, 30, "#ch", 7)
        assert len(payload["blocks"]) <= 50

    def test_fallback_text_contains_count(self):
        groups = [
            {"topic": "CI/CD", "count": 4, "entries": [_make_entry("06.28", "U3", "빌드 실패")]},
        ]
        payload = build_history_grouped_blocks(groups, 4, "#ci", 7)
        assert "4건" in payload["text"]


# ---------------------------------------------------------------------------
# get_channel_history_by_topic 테스트
# ---------------------------------------------------------------------------

class TestGetChannelHistoryByTopic:
    """_group_messages_by_topic 순수 함수 테스트 (DB 의존성 없음)."""

    def _make_msg(self, role, is_q, topic, content, thread_ts=None, message_ts=None, user_id="U1"):
        msg = MagicMock()
        msg.role = role
        msg.is_question = is_q
        msg.topic = topic
        msg.content = content
        msg.thread_ts = thread_ts
        msg.message_ts = message_ts or f"ts_{content[:5]}"
        msg.user_id = user_id
        msg.created_at = datetime(2026, 6, 25, 10, 0, 0)
        return msg

    def _run(self, messages):
        return _group_messages_by_topic(messages)

    def test_empty_db_returns_empty(self):
        groups, total = self._run([])
        assert groups == []
        assert total == 0

    def test_single_question_grouped_by_topic(self):
        msgs = [
            self._make_msg("user", True, "Redis", "Redis 연결 오류", message_ts="ts1"),
        ]
        groups, total = self._run(msgs)
        assert total == 1
        assert len(groups) == 1
        assert groups[0]["topic"] == "Redis"
        assert groups[0]["count"] == 1

    def test_bot_answer_paired_within_thread(self):
        q = self._make_msg("user", True, "배포", "배포 방법은?", thread_ts=None, message_ts="root1")
        a = self._make_msg("bot", False, None, "git push 후 자동 배포됩니다", thread_ts="root1", message_ts="bot1")
        groups, _ = self._run([q, a])
        entry = groups[0]["entries"][0]
        assert entry["a_preview"] is not None
        assert "git push" in entry["a_preview"]

    def test_multiple_topics_sorted_by_count(self):
        msgs = []
        for i in range(3):
            msgs.append(self._make_msg("user", True, "Redis", f"Redis Q{i}", message_ts=f"r{i}"))
        msgs.append(self._make_msg("user", True, "DB", "DB Q", message_ts="d1"))
        groups, total = self._run(msgs)
        assert total == 4
        assert groups[0]["topic"] == "Redis"
        assert groups[1]["topic"] == "DB"

    def test_null_topic_becomes_미분류(self):
        msgs = [self._make_msg("user", True, None, "분류 없는 질문", message_ts="u1")]
        groups, _ = self._run(msgs)
        assert groups[0]["topic"] == "미분류"

    def test_미분류_sorted_last(self):
        msgs = [
            self._make_msg("user", True, None, "분류없음", message_ts="u1"),
            self._make_msg("user", True, "Kubernetes", "K8s 질문", message_ts="k1"),
        ]
        groups, _ = self._run(msgs)
        assert groups[-1]["topic"] == "미분류"

    def test_non_question_messages_ignored(self):
        msgs = [
            self._make_msg("user", False, "Redis", "Redis에 대한 코멘트", message_ts="c1"),
            self._make_msg("bot", False, None, "봇 응답", message_ts="b1"),
        ]
        _, total = self._run(msgs)
        assert total == 0

    def test_max_per_topic_limits_entries(self):
        msgs = [
            self._make_msg("user", True, "Redis", f"Redis Q{i}", message_ts=f"r{i}")
            for i in range(5)
        ]
        groups, total = self._run(msgs)
        assert total == 5
        assert len(groups[0]["entries"]) <= 3  # default max_per_topic=3

    def test_max_topics_caps_result(self):
        msgs = [
            self._make_msg("user", True, f"주제{i}", f"Q{i}", message_ts=f"m{i}")
            for i in range(15)
        ]
        groups, total = self._run(msgs)
        assert total == 15
        assert len(groups) <= 10  # default max_topics=10
