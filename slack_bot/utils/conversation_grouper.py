# 비스레드 @mention 체인 및 시간 윈도우 기반 대화 그룹화 유틸리티
from __future__ import annotations
import re
from datetime import datetime, timedelta

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>", re.IGNORECASE)
_TIME_WINDOW = timedelta(minutes=30)


def _extract_mentions(content: str) -> set[str]:
    return {m.upper() for m in _MENTION_RE.findall(content or "")}


def group_messages_by_conversation(messages: list[dict]) -> list[list[dict]]:
    """
    시간순 정렬된 메시지 목록을 대화 그룹 리스트로 반환한다.

    입력 dict 필수 필드:
        user_id   : str | None   — 발신자 ID (대문자 권장)
        role      : str          — "user" | "bot"
        content   : str          — 메시지 본문 (Slack raw text, <@U...> 포함)
        thread_ts : str | None   — 스레드 부모 ts, 없으면 None
        created_at: datetime

    그룹화 규칙:
    1. thread_ts 있는 메시지 → thread_ts 기준 스레드 그룹
    2. 비스레드 메시지:
       a. content 내 <@USER_ID> 중 기존 오픈 대화 참여자이면 그 그룹에 연결
       b. 직전 비스레드 그룹과 30분 이내이면 연결
       c. 그 외 새 그룹 시작

    반환: 각 원소가 같은 대화에 속하는 메시지 dict 리스트.
    """
    thread_groups: dict[str, list[dict]] = {}
    non_threaded: list[list[dict]] = []
    open_user_to_group: dict[str, int] = {}  # user_id(upper) → non_threaded 인덱스

    for msg in messages:
        ts = msg.get("thread_ts")

        if ts:
            if ts not in thread_groups:
                thread_groups[ts] = []
            thread_groups[ts].append(msg)
            continue

        content = msg.get("content") or ""
        user_id = (msg.get("user_id") or "").upper()
        created_at: datetime | None = msg.get("created_at")
        mentions = _extract_mentions(content)

        # a. @mention 체인 연결
        linked: int | None = None
        for uid in mentions:
            if uid in open_user_to_group:
                linked = open_user_to_group[uid]
                break

        if linked is not None:
            non_threaded[linked].append(msg)
            if user_id:
                open_user_to_group[user_id] = linked
            continue

        # b. 시간 윈도우 fallback
        if non_threaded and created_at:
            last_time: datetime | None = non_threaded[-1][-1].get("created_at")
            if last_time and (created_at - last_time) <= _TIME_WINDOW:
                idx = len(non_threaded) - 1
                non_threaded[idx].append(msg)
                if user_id:
                    open_user_to_group[user_id] = idx
                continue

        # c. 새 그룹 시작
        idx = len(non_threaded)
        non_threaded.append([msg])
        if user_id:
            open_user_to_group[user_id] = idx

    return list(thread_groups.values()) + non_threaded
