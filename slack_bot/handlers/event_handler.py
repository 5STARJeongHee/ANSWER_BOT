# Slack Bolt 이벤트 핸들러 - app_mention 및 message 이벤트 처리
from __future__ import annotations
import logging
import re
import threading
import time
from typing import Optional

from slack_bolt import App

import config
from utils.metrics import MESSAGE_PROCESSED_TOTAL
from db.models import get_session_factory
from db.repository import (
    upsert_message,
    get_thread_starter_user_id,
    get_channel_question_history,
    get_channel_history_by_date,
    get_channel_history_by_topic,
    get_dashboard_stats,
    get_recent_fallbacks,
    get_top_topics,
    seed_product_categories,
    get_products_as_llm_hints,
    get_product_owners,
    set_product_owners,
    find_product_by_alias,
    get_all_product_categories,
    increment_product_question_count,
    get_unowned_products_above_threshold,
    mark_product_notified,
    get_notification_admins,
    add_notification_admin,
    remove_notification_admin,
    get_topic_candidates,
    promote_topic_to_product,
)
from services.classifier import classify_message, extract_topic, extract_topic_and_product, MessageCategory
from services.context_retriever import retrieve_context, format_context_for_prompt, embed_text
from services.llm_service import call_qa
from services.web_search import search_web, format_web_search_for_prompt
from services.slack_service import (
    post_thinking_indicator,
    update_message,
    post_message,
    post_answer,
    post_error,
    send_fallback_message,
    send_greeting_message,
    get_user_display_name,
    fetch_thread_history,
)
from ui.message_blocks import (
    build_intro_blocks,
    build_history_blocks,
    build_history_grouped_blocks,
    build_dashboard_blocks,
    build_topic_candidates_blocks,
)
from services.summarizer import summarize_thread_context
from utils.image_processor import analyze_slack_files
from utils.pii_filter import apply_pii_filter, has_pii
from utils.token_counter import trim_messages_to_budget

logger = logging.getLogger(__name__)

# _process_question의 rag_channel_id 기본값 sentinel — 미전달 시 channel_id와 동일하게 동작
_RAG_CHANNEL_DEFAULT = object()

# ---------------------------------------------------------------------------
# 백필 명령 처리
# ---------------------------------------------------------------------------
_BACKFILL_KEYWORDS = ("백필", "backfill", "재수집")
_BACKFILL_DEFAULT_DAYS = 90
_BACKFILL_MAX_DAYS = 365

# 동시 백필 실행 방지 플래그
_backfill_running = threading.Event()


def _parse_backfill_days(text: str) -> int:
    """
    '7일', '2주', '한달', '3개월', '90' 등 자연어 기간 표현을 일수로 변환한다.
    인식 불가 시 기본값(_BACKFILL_DEFAULT_DAYS)을 반환한다.
    """
    t = text.strip().lower().replace(" ", "")

    # 숫자만 (e.g. "30", "90")
    if t.isdigit():
        return min(int(t), _BACKFILL_MAX_DAYS)

    # 주 단위: "2주", "1주일"
    import re
    m = re.match(r"(\d+)\s*주", t)
    if m:
        return min(int(m.group(1)) * 7, _BACKFILL_MAX_DAYS)

    # 개월 단위: "1개월", "3달", "한달"
    if t in ("한달", "1달", "1개월"):
        return 30
    if t in ("두달", "2달", "2개월"):
        return 60
    if t in ("세달", "3달", "3개월"):
        return 90
    m = re.match(r"(\d+)\s*(?:개월|달)", t)
    if m:
        return min(int(m.group(1)) * 30, _BACKFILL_MAX_DAYS)

    # 일 단위: "7일", "14일"
    m = re.match(r"(\d+)\s*일", t)
    if m:
        return min(int(m.group(1)), _BACKFILL_MAX_DAYS)

    # 별도 표현
    if t in ("일주일", "1주일"):
        return 7
    if t in ("오늘", "today"):
        return 1
    if t in ("전체", "all"):
        return _BACKFILL_MAX_DAYS

    return _BACKFILL_DEFAULT_DAYS


def _is_backfill_command(text: str) -> tuple[bool, int]:
    """
    텍스트가 백필 명령인지 확인한다.
    반환: (is_backfill: bool, days: int)
    명령 형식: 백필 [기간]  (기간 생략 시 기본값 90일)
    예) "백필", "백필 7일", "backfill 2주", "재수집 한달", "백필 30"
    """
    stripped = text.strip()
    lower = stripped.lower()
    for kw in _BACKFILL_KEYWORDS:
        if lower == kw or lower.startswith(kw + " ") or lower.startswith(kw + "\n"):
            rest = stripped[len(kw):].strip()
            days = _parse_backfill_days(rest) if rest else _BACKFILL_DEFAULT_DAYS
            return True, days
    return False, 0


# ---------------------------------------------------------------------------
# 히스토리 명령 처리
# ---------------------------------------------------------------------------
_HISTORY_KEYWORDS = (
    "히스토리", "history", "질문 목록", "질문목록", "지난 대화 목록", "대화 목록 요약",
    "질문 이력", "질문이력", "대화 목록", "대화목록", "이력", "채널 이력", "채널이력",
)


def _is_history_command(text: str) -> tuple[bool, int]:
    """텍스트가 질문 이력 조회 명령인지 확인한다.
    반환: (is_history: bool, days: int) — 기간 미지정 시 기본값 7일.
    """
    stripped = text.strip()
    lower = stripped.lower()
    for kw in _HISTORY_KEYWORDS:
        kw_nospace = kw.replace(" ", "")
        lower_nospace = lower.replace(" ", "")
        if lower_nospace == kw_nospace or lower_nospace.startswith(kw_nospace):
            rest = stripped[len(kw):].strip()
            days = _parse_backfill_days(rest) if rest else 7
            return True, days
    return False, 0


# ---------------------------------------------------------------------------
# 대시보드 명령 처리
# ---------------------------------------------------------------------------
_DASHBOARD_KEYWORDS = (
    "대시보드", "dashboard", "통계", "stats", "현황", "봇 통계", "봇통계", "이용 현황", "이용현황",
)


def _is_dashboard_command(text: str) -> tuple[bool, int]:
    """
    텍스트가 대시보드 명령인지 확인한다.
    반환: (is_dashboard: bool, days: int)
    """
    stripped = text.strip()
    lower = stripped.lower()
    for kw in _DASHBOARD_KEYWORDS:
        kw_lower = kw.lower()
        if lower == kw_lower or lower.startswith(kw_lower + " ") or lower.startswith(kw_lower + "\n"):
            rest = stripped[len(kw):].strip()
            days = _parse_backfill_days(rest) if rest else 7
            return True, days
    return False, 0


# ---------------------------------------------------------------------------
# 담당자 관리 명령 처리
# ---------------------------------------------------------------------------
_OWNER_LIST_KEYWORDS = ("담당자 목록", "담당자목록", "owner list", "담당자 리스트", "제품 담당자")
_OWNER_SET_PREFIX = ("담당자 설정", "담당자설정", "owner set")
_OWNER_DELETE_PREFIX = ("담당자 삭제", "담당자삭제", "owner delete", "담당자 제거")

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")


def _parse_owner_command(text: str) -> tuple[str, Optional[str], list[str]]:
    """담당자 관리 명령을 파싱한다.
    반환: ("list"|"set"|"delete"|"none", product_key_or_alias, owner_ids)
    """
    stripped = text.strip()
    lower_nospace = stripped.lower().replace(" ", "")

    for kw in _OWNER_LIST_KEYWORDS:
        if lower_nospace == kw.replace(" ", "") or lower_nospace.startswith(kw.replace(" ", "")):
            return "list", None, []

    for kw in _OWNER_SET_PREFIX:
        if lower_nospace.startswith(kw.replace(" ", "")):
            rest = stripped[len(kw):].strip()
            parts = rest.split()
            if not parts:
                return "set", None, []
            product_identifier = parts[0]
            owner_ids = _MENTION_RE.findall(rest)
            return "set", product_identifier, owner_ids

    for kw in _OWNER_DELETE_PREFIX:
        if lower_nospace.startswith(kw.replace(" ", "")):
            rest = stripped[len(kw):].strip().split()
            product_identifier = rest[0] if rest else None
            return "delete", product_identifier, []

    return "none", None, []


def _handle_owner_command(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    question: str,
    session_factory,
) -> bool:
    """담당자 관리 명령을 처리한다. 처리 시 True, 아니면 False 반환."""
    import json as _json
    action, product_identifier, owner_ids = _parse_owner_command(question)
    if action == "none":
        return False

    session = session_factory()
    try:
        if action == "list":
            cats = get_all_product_categories(session)
            if not cats:
                post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                             text="등록된 제품이 없습니다.")
                return True
            lines = ["*제품 담당자 목록*"]
            for c in cats:
                owners = _json.loads(c.owner_user_ids_json or "[]")
                owner_str = " ".join(f"<@{u}>" for u in owners) if owners else "_미지정_"
                lines.append(f"• `{c.product_key}` ({c.display_name}) — {owner_str} · 질문 {c.question_count}건")
            post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                         text="\n".join(lines))
            return True

        if product_identifier is None:
            post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                         text="제품 키를 입력해주세요. 예: `담당자 설정 iruda_backend @담당자`")
            return True

        from db.repository import get_product_category as _get_cat
        cat = _get_cat(session, product_identifier)
        if cat is None:
            cat = find_product_by_alias(session, product_identifier)
        if cat is None:
            post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                         text=f"제품 `{product_identifier}`을 찾을 수 없습니다. `담당자 목록`으로 확인하세요.")
            return True

        if action == "set":
            if not owner_ids:
                post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                             text="담당자를 @멘션으로 지정해주세요. 예: `담당자 설정 iruda_backend @담당자1 @담당자2`")
                return True
            set_product_owners(session, cat.product_key, owner_ids)
            session.commit()
            mention_str = " ".join(f"<@{u}>" for u in owner_ids)
            post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                         text=f"`{cat.display_name}` 담당자가 {mention_str} 으로 설정되었습니다.")
        elif action == "delete":
            set_product_owners(session, cat.product_key, [])
            session.commit()
            post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                         text=f"`{cat.display_name}` 담당자가 제거되었습니다.")
        return True
    finally:
        session.close()


_NOTIF_ADMIN_LIST_KEYWORDS = ("알림관리자 목록", "알림 관리자 목록", "알림관리자목록", "notifyadmin list")
_NOTIF_ADMIN_ADD_PREFIX = ("알림관리자 추가", "알림 관리자 추가", "알림관리자추가", "notifyadmin add")
_NOTIF_ADMIN_DEL_PREFIX = ("알림관리자 삭제", "알림 관리자 삭제", "알림관리자삭제", "notifyadmin delete")


def _handle_notification_admin_command(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    question: str,
    session_factory,
) -> bool:
    """알림 관리자 관리 명령을 처리한다. 처리 시 True, 아니면 False 반환."""
    stripped = question.strip()
    lower_nospace = stripped.lower().replace(" ", "")

    session = session_factory()
    try:
        # 목록 조회
        for kw in _NOTIF_ADMIN_LIST_KEYWORDS:
            if lower_nospace.startswith(kw.replace(" ", "")):
                admins = get_notification_admins(session)
                if not admins:
                    post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                                 text="등록된 알림 관리자가 없습니다. `알림관리자 추가 @유저명`으로 추가하세요.")
                else:
                    mention_str = " ".join(f"<@{u}>" for u in admins)
                    post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                                 text=f"*알림 관리자 목록*\n{mention_str}")
                return True

        # 추가
        for kw in _NOTIF_ADMIN_ADD_PREFIX:
            if lower_nospace.startswith(kw.replace(" ", "")):
                user_ids = _MENTION_RE.findall(stripped)
                if not user_ids:
                    post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                                 text="추가할 관리자를 @멘션으로 입력하세요. 예: `알림관리자 추가 @유저명`")
                    return True
                added, already = [], []
                for uid in user_ids:
                    if add_notification_admin(session, uid):
                        added.append(uid)
                    else:
                        already.append(uid)
                session.commit()
                parts = []
                if added:
                    parts.append(" ".join(f"<@{u}>" for u in added) + " 추가됨")
                if already:
                    parts.append(" ".join(f"<@{u}>" for u in already) + " 이미 등록됨")
                post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                             text="알림 관리자 " + ", ".join(parts) + ".")
                return True

        # 삭제
        for kw in _NOTIF_ADMIN_DEL_PREFIX:
            if lower_nospace.startswith(kw.replace(" ", "")):
                user_ids = _MENTION_RE.findall(stripped)
                if not user_ids:
                    post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                                 text="삭제할 관리자를 @멘션으로 입력하세요. 예: `알림관리자 삭제 @유저명`")
                    return True
                removed, not_found = [], []
                for uid in user_ids:
                    if remove_notification_admin(session, uid):
                        removed.append(uid)
                    else:
                        not_found.append(uid)
                session.commit()
                parts = []
                if removed:
                    parts.append(" ".join(f"<@{u}>" for u in removed) + " 삭제됨")
                if not_found:
                    parts.append(" ".join(f"<@{u}>" for u in not_found) + " 미등록 상태")
                post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                             text="알림 관리자 " + ", ".join(parts) + ".")
                return True

        return False
    except Exception as e:
        logger.error(f"알림 관리자 명령 처리 오류: {e}", exc_info=True)
        post_message(client=client, channel=channel_id, thread_ts=thread_ts,
                     text=f"알림 관리자 명령 처리 중 오류가 발생했습니다: {e}")
        return True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 제품 후보 명령 처리 (B방향 동적 발견)
# ---------------------------------------------------------------------------
_CANDIDATE_LIST_KEYWORDS = ("제품 후보", "product candidates", "미분류 주제")
_CANDIDATE_PROMOTE_PREFIX = ("제품 등록", "product add")

_TOPIC_KEY_RE = re.compile(r"[^\w가-힣]")


def _topic_to_product_key(topic: str) -> str:
    """topic 이름을 product_key 슬러그로 변환한다."""
    key = topic.strip().lower()
    key = re.sub(r"\s+", "_", key)
    key = _TOPIC_KEY_RE.sub("", key)
    return key[:50] or "product"


def _parse_product_candidate_command(question: str) -> tuple[str, Optional[str]]:
    """반환: ("list"|"promote"|"none", topic_or_None)"""
    lower_q = question.strip().lower()
    for kw in _CANDIDATE_LIST_KEYWORDS:
        if lower_q == kw.lower() or lower_q.startswith(kw.lower() + " "):
            return "list", None
    for kw in _CANDIDATE_PROMOTE_PREFIX:
        if lower_q.startswith(kw.lower()):
            topic = question.strip()[len(kw):].strip()
            return "promote", topic if topic else None
    return "none", None


def _handle_product_candidate_command(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    question: str,
    session_factory,
) -> bool:
    """제품 후보 조회·등록 명령을 처리한다. 처리 시 True, 아니면 False 반환."""
    action, topic = _parse_product_candidate_command(question)
    if action == "none":
        return False

    session = session_factory()
    try:
        if action == "list":
            candidates = get_topic_candidates(session, min_count=5)
            payload = build_topic_candidates_blocks(candidates)
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=payload["text"],
                blocks=payload["blocks"],
            )
            return True

        if action == "promote":
            if not topic:
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="등록할 주제명을 입력하세요. 예: `제품 등록 Redis 연결 오류`",
                )
                return True
            product_key = _topic_to_product_key(topic)
            ok = promote_topic_to_product(session, topic, product_key, topic)
            if ok:
                session.commit()
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=(
                        f"✅ `{topic}` 을(를) 제품 `{product_key}`으로 등록했습니다.\n"
                        f"`담당자 설정 {product_key} @담당자` 로 담당자를 지정할 수 있습니다."
                    ),
                )
            else:
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"⚠️ 제품 키 `{product_key}`이 이미 존재합니다.",
                )
            return True

        return False
    except Exception as e:
        logger.error(f"제품 후보 명령 처리 오류: {e}", exc_info=True)
        post_message(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"제품 후보 명령 처리 중 오류가 발생했습니다: {e}",
        )
        return True
    finally:
        session.close()


def _notify_admin_unowned_products(client, session_factory) -> None:
    """담당자 미지정 제품 중 질문이 임계값 이상이면 알림 관리자에게 DM을 보낸다."""
    import datetime
    session = session_factory()
    try:
        admin_ids = get_notification_admins(session)
        if not admin_ids:
            return

        candidates = get_unowned_products_above_threshold(session, threshold=5)
        notifiable = [
            c for c in candidates
            if c.notified_at is None or (
                datetime.datetime.utcnow() - c.notified_at
            ).total_seconds() > 86400
        ]

        if not notifiable:
            return

        lines = [":bell: *담당자 지정 요청*\n아래 제품에 5건 이상 질문이 쌓였습니다. 담당자를 지정해 주세요."]
        for c in notifiable:
            lines.append(f"• `{c.product_key}` ({c.display_name}) — 질문 {c.question_count}건")
        lines.append("\n`@QNA BOT 담당자 설정 [제품키] @담당자1 @담당자2` 로 지정하세요.")
        msg = "\n".join(lines)

        for admin_id in admin_ids:
            try:
                resp = client.conversations_open(users=[admin_id])
                dm_channel = resp["channel"]["id"]
                client.chat_postMessage(channel=dm_channel, text=msg)
                logger.info(f"관리자 DM 발송: admin={admin_id} products={[c.product_key for c in notifiable]}")
            except Exception as e:
                logger.warning(f"관리자 DM 실패: admin={admin_id} error={e}")

        for c in notifiable:
            mark_product_notified(session, c.product_key)
        session.commit()
    except Exception as e:
        logger.warning(f"관리자 알림 처리 중 오류: {e}")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 요약 주기 명령 처리
# ---------------------------------------------------------------------------
_SCHEDULE_SET_KEYWORDS = ("요약 주기 설정", "요약주기설정", "summary schedule")
_SCHEDULE_VIEW_KEYWORDS = ("요약 주기 확인", "요약주기확인", "요약 주기", "summary schedule check")

_WEEKDAY_MAP: dict[str, int] = {
    "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
    "월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4, "토요일": 5, "일요일": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _parse_hour(text: str) -> Optional[int]:
    """텍스트에서 시각(0~23)을 추출한다. 예: '3시', '오전 2시', '14시'"""
    m = re.search(r"(\d{1,2})\s*시", text)
    if m:
        h = int(m.group(1))
        return h if 0 <= h <= 23 else None
    return None


def _parse_schedule_config(text: str) -> Optional[dict]:
    """
    자연어 주기 표현을 schedule config dict로 변환한다.
    예: "매일 3시" → {"type": "daily", "hour": 3}
        "매주 월요일 2시" → {"type": "weekly", "weekday": 0, "hour": 2}
        "매월 1일 2시" → {"type": "monthly", "day": 1, "hour": 2}
    인식 불가 시 None 반환.
    """
    t = text.strip().lower()
    hour = _parse_hour(t) if _parse_hour(t) is not None else 2

    if "매일" in t or "daily" in t or "every day" in t:
        return {"type": "daily", "hour": hour}

    if "매월" in t or "monthly" in t or "every month" in t:
        m = re.search(r"(\d{1,2})\s*일", t)
        day = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 31 else 1
        return {"type": "monthly", "day": day, "hour": hour}

    if "매주" in t or "weekly" in t or "every week" in t:
        weekday = 0
        for name, idx in _WEEKDAY_MAP.items():
            if name in t:
                weekday = idx
                break
        return {"type": "weekly", "weekday": weekday, "hour": hour}

    return None


def _is_schedule_command(text: str) -> tuple[bool, bool]:
    """
    텍스트가 스케줄 명령인지 확인한다.
    반환: (is_schedule: bool, is_view_only: bool)
    """
    lower = text.strip().lower()
    is_set = any(lower.startswith(kw.lower()) for kw in _SCHEDULE_SET_KEYWORDS)
    if is_set:
        return True, False
    is_view = any(
        lower == kw.lower().replace(" ", "") or lower.startswith(kw.lower())
        for kw in _SCHEDULE_VIEW_KEYWORDS
    )
    return is_view, True


# ---------------------------------------------------------------------------
# 정규화 명령 처리
# ---------------------------------------------------------------------------
_NORMALIZE_KEYWORDS = ("정규화 실행", "topic 정규화", "토픽 정규화", "normalize topics")

# 동시 정규화 실행 방지 플래그
_normalize_running = threading.Event()


def _is_normalize_command(text: str) -> bool:
    """텍스트가 topic 정규화 실행 명령인지 확인한다."""
    lower = text.strip().lower().replace(" ", "")
    return any(kw.replace(" ", "") in lower for kw in _NORMALIZE_KEYWORDS)


def _run_normalize_in_background(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    session_factory,
) -> None:
    """
    topic 정규화 배치를 백그라운드 스레드로 실행하고 Slack에 결과를 전송한다.
    동시 실행 방지: 이미 실행 중이면 즉시 반환한다.
    """
    if _normalize_running.is_set():
        post_message(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            text="⏳ 이미 정규화가 실행 중입니다. 완료 후 다시 시도해 주세요.",
        )
        return

    post_message(
        client=client,
        channel=channel_id,
        thread_ts=thread_ts,
        text="🔄 *topic 정규화 시작*\nLLM 그룹핑 중입니다. 잠시 기다려 주세요.",
    )

    def worker():
        _normalize_running.set()
        try:
            from batch.topic_normalizer import run_normalize_batch
            stats = run_normalize_batch(session_factory)
            if stats["errors"] > 0 and stats["rows_updated"] == 0:
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="❌ *정규화 실패*\nLLM 호출 오류가 발생했습니다. 로그를 확인해 주세요.",
                )
            else:
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=(
                        f"✅ *topic 정규화 완료*\n"
                        f"• distinct topic: *{stats['distinct_topics']}개*\n"
                        f"• 통합 후 그룹: *{stats['groups_formed']}개*\n"
                        f"• 행 업데이트: *{stats['rows_updated']}건*"
                    ),
                )
        except Exception as exc:
            logger.error(f"정규화 백그라운드 오류: {exc}", exc_info=True)
            post_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"❌ *정규화 오류*: {exc}",
            )
        finally:
            _normalize_running.clear()

    threading.Thread(target=worker, daemon=True, name="slack-cmd-normalize").start()


# ---------------------------------------------------------------------------
# 소개 / 도움말 명령 처리
# ---------------------------------------------------------------------------
_INTRO_KEYWORDS = (
    # 자기소개
    "자기소개", "소개해줘", "소개 해줘", "소개좀", "소개 좀",
    "봇 소개", "너 누구야", "넌 누구야", "너는 누구",
    # 사용법
    "사용법", "어떻게 써", "어떻게 사용", "사용 방법", "쓰는 법", "쓰는법",
    "어떻게 쓰는", "어떻게쓰는",
    # 기능
    "기능이 뭐야", "기능 알려줘", "뭘 할 수 있어", "무엇을 할 수 있", "뭐 할 수 있",
    "무슨 기능", "어떤 기능",
    # 도움말
    "도움말", "help", "명령어",
    # 소개 단독
    "소개",
)


def _is_intro_command(text: str) -> bool:
    """텍스트가 봇 소개·사용법·도움말 요청인지 확인한다."""
    lower = text.strip().lower().replace(" ", "")
    return any(kw.replace(" ", "") in lower for kw in _INTRO_KEYWORDS)


def _run_backfill_in_background(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    days: int,
    session_factory,
) -> None:
    """
    채널별 백필을 백그라운드 스레드로 실행하고 Slack에 진행 상황을 전송한다.
    동시 실행 방지: 이미 실행 중이면 즉시 반환한다.
    """
    from batch.collector import backfill_channel
    from slack_sdk import WebClient

    if _backfill_running.is_set():
        post_message(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            text="⏳ 이미 백필이 실행 중입니다. 완료 후 다시 시도해 주세요.",
        )
        return

    if not config.TARGET_CHANNEL_IDS:
        post_message(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            text="⚠️ TARGET_CHANNEL_IDS가 설정되지 않아 백필할 채널이 없습니다.",
        )
        return

    post_message(
        client=client,
        channel=channel_id,
        thread_ts=thread_ts,
        text=(
            f"🔄 *백필 시작*\n"
            f"• 기간: 최근 *{days}일*\n"
            f"• 대상 채널: {len(config.TARGET_CHANNEL_IDS)}개\n"
            f"채널별 완료 시 결과를 알려드립니다."
        ),
    )

    def worker():
        _backfill_running.set()
        try:
            bf_client = WebClient(token=config.SLACK_BOT_TOKEN)
            total = 0
            failed = 0
            last_progress_at: dict[str, float] = {}

            def notify_progress(progress: dict) -> None:
                ch_id = progress["channel_id"]
                now = time.monotonic()
                previous = last_progress_at.get(ch_id, 0.0)
                if progress["page"] != 1 and now - previous < 30:
                    return
                last_progress_at[ch_id] = now
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=(
                        f"  ⏱️ <#{ch_id}> 진행 중 — "
                        f"{progress['page']}페이지 처리, "
                        f"{progress['fetched_total']}건 확인, "
                        f"{progress['saved_total']}건 저장"
                    ),
                )

            for ch_id in config.TARGET_CHANNEL_IDS:
                try:
                    count = backfill_channel(
                        client=bf_client,
                        session_factory=session_factory,
                        channel_id=ch_id,
                        days=days,
                        force=True,  # 슬랙 명령 백필은 항상 강제 재수집
                        progress_callback=notify_progress,
                    )
                    total += count
                    post_message(
                        client=client,
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=f"  ✅ <#{ch_id}> — {count}건 수집 완료",
                    )
                except Exception as exc:
                    failed += 1
                    logger.error(f"백필 오류 (channel={ch_id}): {exc}", exc_info=True)
                    post_message(
                        client=client,
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=f"  ❌ <#{ch_id}> — 오류 발생: {exc}",
                    )

            summary = f"🎉 *백필 완료!* 총 *{total}건* 수집"
            if failed:
                summary += f" (실패 채널 {failed}개)"
            post_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
                text=summary,
            )
        finally:
            _backfill_running.clear()

    threading.Thread(target=worker, daemon=True, name="slack-cmd-backfill").start()

# ---------------------------------------------------------------------------
# 이벤트 중복 방지 (Socket Mode는 기본 2개 연결로 동일 이벤트를 두 번 전달함)
# ---------------------------------------------------------------------------
_processed_events: dict[str, float] = {}
_event_lock = threading.Lock()
_EVENT_DEDUP_TTL = 60.0  # 초 — 이 시간 내 동일 event_id는 한 번만 처리


def _is_duplicate_event(event_id: str) -> bool:
    """동일 event_id가 TTL 내에 이미 처리됐으면 True를 반환한다."""
    now = time.monotonic()
    with _event_lock:
        expired = [k for k, v in _processed_events.items() if now - v > _EVENT_DEDUP_TTL]
        for k in expired:
            del _processed_events[k]
        if event_id in _processed_events:
            return True
        _processed_events[event_id] = now
        return False


# ---------------------------------------------------------------------------
# QA 프롬프트 상수
# ---------------------------------------------------------------------------
_QA_SYSTEM_PROMPT = (
    "너는 사내 업무 지원 Slack 챗봇이다. 질문이 영어로 작성된 경우 영어로, 그 외에는 한국어로 답변하라.\n"
    "답변은 반드시 아래 [참고 컨텍스트 - 과거 관련 대화] 또는 [최근 대화 이력]에 근거해야 한다. "
    "그 중 [사람 답변]으로 표시된 내용이 가장 신뢰할 수 있는 근거다.\n"
    "절대 훈련 데이터의 일반 지식으로 추측하거나 설명하지 마라. "
    "컨텍스트가 '(관련 과거 대화 없음)'이거나 질문과 관련 없는 내용뿐이라면 "
    "'이 문제에 대한 사내 정보가 아직 없습니다. "
    "오류 메시지 전체·스택 트레이스·관련 로그를 공유해 주시면 담당자가 더 정확히 파악할 수 있습니다.'라고 답하라.\n"
    "답변 길이: 설정 절차·코드·파일 경로가 필요한 기술 질문은 단계별 목록과 코드블록을 포함하라. "
    "그 외에는 3~5문장으로 핵심만 전달하라.\n"
    "AI 생성 답변임을 사용자가 인지할 수 있도록 답변 끝에 '[AI 생성 답변]'을 덧붙인다."
)

# 웹 검색 결과가 있을 때 사용하는 시스템 프롬프트.
# 과거 대화(특히 사람 답변)를 1차 근거로, 웹 검색을 보조로 사용한다.
_QA_SYSTEM_PROMPT_WITH_WEB = (
    "너는 사내 업무 지원 Slack 챗봇이다. 질문이 영어로 작성된 경우 영어로, 그 외에는 한국어로 답변하라.\n"
    "답변 근거 우선순위: "
    "① [과거 관련 대화]의 [사람 답변] — 실제 사람이 직접 작성한 답변으로 가장 신뢰도가 높다. "
    "② [과거 관련 대화]의 [봇 답변] — 이전 AI 답변으로 참고할 수 있다. "
    "③ [웹 검색 결과] — 과거 대화만으로 답하기 어려울 때만 보조로 활용하라.\n"
    "절대 훈련 데이터의 일반 지식으로 추측하거나 설명하지 마라. "
    "위 세 가지 근거가 모두 질문과 무관하거나 없다면 "
    "'이 문제에 대한 사내 정보가 아직 없습니다. "
    "오류 메시지 전체·스택 트레이스·관련 로그를 공유해 주시면 담당자가 더 정확히 파악할 수 있습니다.'라고 답하라.\n"
    "답변 길이: 설정 절차·코드·파일 경로가 필요한 기술 질문은 단계별 목록과 코드블록을 포함하라. "
    "그 외에는 3~5문장으로 핵심만 전달하라.\n"
    "AI 생성 답변임을 사용자가 인지할 수 있도록 답변 끝에 '[AI 생성 답변]'을 덧붙인다."
)


# ---------------------------------------------------------------------------
# 보조 함수
# ---------------------------------------------------------------------------

def _clean_mention_text(text: str, bot_user_id: str) -> str:
    """@봇 멘션 텍스트에서 멘션 태그를 제거한다."""
    return text.replace(f"<@{bot_user_id}>", "").strip()


_SLACK_USER_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def _has_user_mention(text: str) -> bool:
    """메시지에 Slack 사용자 멘션(<@UXXXX>)이 포함됐는지 확인한다."""
    return bool(_SLACK_USER_MENTION_RE.search(text))


def _build_image_context(event: dict, bot_token: str) -> str:
    """이벤트의 이미지를 분석하여 텍스트를 반환한다 (analyze_slack_files 위임)."""
    return analyze_slack_files(event.get("files") or [], bot_token)


_FALLBACK_TRIGGER_KEYWORDS = (
    "확인이 필요합니다",
    "확인 필요합니다",
    "담당자에게 문의",
    "담당자 문의",
    "알 수 없습니다",
)


def _evaluate_answer(question: str, draft_answer: str) -> bool:
    """
    답변 텍스트에 불확실성 키워드가 있으면 False를 반환한다.
    QA 프롬프트가 "모르면 확인이 필요합니다라고 답하라"고 지시하므로
    추가 LLM 호출 없이 규칙 기반으로 판단한다.
    """
    return not any(kw in draft_answer for kw in _FALLBACK_TRIGGER_KEYWORDS)


def _save_message_and_embed(
    session_factory,
    *,
    event_id: Optional[str],
    channel_id: str,
    thread_ts: Optional[str],
    message_ts: str,
    user_id: Optional[str],
    role: str,
    content: str,
    is_question: Optional[bool] = None,
    is_fallback: bool = False,
    response_time_ms: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    rag_avg_similarity: Optional[float] = None,
    used_web_search: bool = False,
    topic: Optional[str] = None,
    product_key: Optional[str] = None,
) -> Optional[int]:
    """
    메시지를 저장하고 임베딩을 생성한다.
    트랜잭션을 두 단계로 분리하여 CPU 집약적인 임베딩 작업 동안
    DB 연결을 점유하지 않도록 한다.
    """
    from db.repository import save_embedding

    pii_flagged = has_pii(content)
    clean_content = apply_pii_filter(content)

    # 1단계: 메시지 저장 (짧은 트랜잭션)
    msg_id: Optional[int] = None
    session = session_factory()
    try:
        msg = upsert_message(
            session=session,
            event_id=event_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            user_id=user_id,
            role=role,
            content=clean_content,
            is_question=is_question,
            is_fallback=is_fallback,
            response_time_ms=response_time_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            rag_avg_similarity=rag_avg_similarity,
            used_web_search=used_web_search,
            topic=topic,
            product_key=product_key,
        )
        if msg is None:
            session.commit()
            return None
        msg_id = msg.id
        session.commit()

        if pii_flagged:
            logger.info(f"PII 마스킹 적용 완료 (message_id={msg_id})")

    except Exception as exc:
        session.rollback()
        logger.error(f"메시지 저장 실패: {exc}", exc_info=True)
        return None
    finally:
        session.close()

    # 2단계: 개별 메시지 임베딩 (너무 짧으면 생략하되, 이후 단계는 계속 진행한다)
    if len(clean_content.strip()) < config.EMBED_MIN_CHARS:
        logger.debug(f"임베딩 생략 — 메시지 너무 짧음 ({len(clean_content)}자 < {config.EMBED_MIN_CHARS}, id={msg_id})")
    else:
        embedding = embed_text(clean_content)
        session2 = session_factory()
        try:
            save_embedding(
                session=session2,
                source_message_id=msg_id,
                chunk_text=clean_content,
                embedding=embedding,
            )
            session2.commit()
        except Exception as exc:
            session2.rollback()
            logger.warning(f"임베딩 저장 실패 (message_id={msg_id}): {exc}", exc_info=True)
        finally:
            session2.close()

    # 3단계: thread 청크 갱신 (스레드 메시지이고 ENABLE_THREAD_CHUNKING이면)
    if thread_ts and config.ENABLE_THREAD_CHUNKING:
        from db.repository import save_thread_chunk_embedding
        session3 = session_factory()
        try:
            save_thread_chunk_embedding(
                session=session3,
                channel_id=channel_id,
                thread_ts=thread_ts,
                embed_fn=embed_text,
            )
            session3.commit()
        except Exception as exc:
            session3.rollback()
            logger.warning(f"Thread 청크 저장 실패 (thread_ts={thread_ts}): {exc}", exc_info=True)
        finally:
            session3.close()

    # 4단계: session 윈도우 청크 갱신 (비스레드 메시지이고 ENABLE_SESSION_CHUNKING이면)
    if not thread_ts and config.ENABLE_SESSION_CHUNKING:
        from db.repository import save_session_window_embedding
        session4 = session_factory()
        try:
            save_session_window_embedding(
                session=session4,
                channel_id=channel_id,
                current_message_ts=message_ts,
                embed_fn=embed_text,
                window_minutes=config.SESSION_WINDOW_MINUTES,
            )
            session4.commit()
        except Exception as exc:
            session4.rollback()
            logger.warning(f"Session 청크 저장 실패 (message_ts={message_ts}): {exc}", exc_info=True)
        finally:
            session4.close()

    return msg_id


def _process_question(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    message_ts: str,
    question: str,
    user_id: Optional[str],
    user_name: str,
    session_factory,
    thinking_ts: Optional[str] = None,
    thread_summary: Optional[str] = None,
    rag_channel_id=_RAG_CHANNEL_DEFAULT,
    image_context: Optional[str] = None,
    show_thread_tip: bool = False,
    product_key: Optional[str] = None,
    topic: Optional[str] = None,
) -> None:
    """
    LLM으로 질문에 답변을 생성하고 Slack에 전송한다.
    threading.Thread 내에서 실행되므로 독립 세션을 사용한다.
    rag_channel_id: RAG 검색 범위. 미전달 시 channel_id와 동일, None이면 전체 채널 검색 (DM용).
    """
    # rag_channel_id 미전달이면 현재 채널, None이면 전체 채널 (DM에서 호출 시)
    effective_rag_channel = channel_id if rag_channel_id is _RAG_CHANNEL_DEFAULT else rag_channel_id

    from utils.token_counter import estimate_tokens
    _start_time = time.monotonic()

    session = session_factory()
    try:
        # 1. RAG 컨텍스트 검색
        contexts = retrieve_context(
            session=session,
            question=question,
            channel_id=effective_rag_channel,
            thread_summary=thread_summary,
            image_context=image_context,
            topic=topic,
        )
        context_text = format_context_for_prompt(contexts)

        # 2. 최근 메시지 조회 (웹 검색과 독립적, DB 조회)
        from db.repository import get_recent_messages, has_negative_feedback
        recent_msgs = get_recent_messages(
            session=session,
            channel_id=channel_id,
            thread_ts=thread_ts,
            limit=config.RECENT_MESSAGE_COUNT,
        )
        recent_text = "\n".join(
            f"[{'봇' if m.role == 'bot' else '사용자'}]: {m.content}"
            for m in recent_msgs
        )

        # 3. 부정 피드백 컨텍스트 제거 + 유사도 기반 웹 검색 스킵 결정
        top_bot_contexts = [
            c for c in contexts
            if c.get("similarity", 0.0) >= 0.90 and c.get("role") == "bot"
        ]
        if top_bot_contexts:
            negative_ids = {
                c["message_id"]
                for c in top_bot_contexts
                if c.get("message_id") and has_negative_feedback(session, c["message_id"])
            }
            if negative_ids:
                contexts = [c for c in contexts if c.get("message_id") not in negative_ids]
                context_text = format_context_for_prompt(contexts)
                logger.info(f"부정 피드백 컨텍스트 {len(negative_ids)}건 제외")

        highest_similarity = max((c.get("similarity", 0.0) for c in contexts), default=0.0)
        _rag_avg_sim = (
            sum(c.get("similarity", 0.0) for c in contexts) / len(contexts)
            if contexts else None
        )
        web_search_block = ""

        if highest_similarity >= 0.90:
            logger.info(f"RAG 충분({highest_similarity:.2f}), 웹 검색 생략")
        else:
            web_search_text = search_web(question)
            web_search_block = format_web_search_for_prompt(web_search_text)

        # 4. QA 프롬프트 구성 (웹 검색 결과는 RAG 컨텍스트 뒤에 배치)
        system_prompt = _QA_SYSTEM_PROMPT_WITH_WEB if web_search_block else _QA_SYSTEM_PROMPT
        user_message_content = (
            f"[참고 컨텍스트 - 과거 관련 대화]\n{context_text}\n\n"
            + (f"[스레드 이전 문맥 요약]\n{thread_summary}\n\n" if thread_summary else "")
            + (f"{web_search_block}\n\n" if web_search_block else "")
            + f"[최근 대화 이력]\n{recent_text or '(없음)'}\n\n"
            f"[현재 질문]\n작성자: {user_name}\n내용: {question}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message_content},
        ]

        # 5. 토큰 예산 내로 메시지 정리
        messages_trimmed = trim_messages_to_budget(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=config.MAX_CONTEXT_TOKENS,
        )
        # 입력 토큰 추정 (LLM에 전달하기 직전)
        from utils.token_counter import estimate_message_tokens
        _prompt_tokens = estimate_message_tokens(messages_trimmed)

        # 6. 답변 생성
        answer = call_qa(messages_trimmed)
        if not answer:
            logger.error("QA 모델에서 답변 생성 실패")
            _send_error_or_fallback(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                question=question,
                thinking_ts=thinking_ts,
            )
            return

        # 7. Fallback 평가
        can_answer = _evaluate_answer(question, answer)
        if not can_answer:
            logger.info(f"Fallback 판단: 답변 불확실 (question={question[:50]!r})")
            # 제품 담당자 조회 (product_key가 있는 경우)
            _fallback_owners: list[str] = []
            if product_key:
                _owner_session = session_factory()
                try:
                    _fallback_owners = get_product_owners(_owner_session, product_key)
                finally:
                    _owner_session.close()
            send_fallback_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
                question=question,
                fallback_user_ids=_fallback_owners,
                thinking_ts=thinking_ts,
            )
            # Fallback 이력 저장
            _save_message_and_embed(
                session_factory=session_factory,
                event_id=None,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=f"fallback_{message_ts}",
                user_id=None,
                role="bot",
                content="[FALLBACK] " + question[:100],
                is_fallback=True,
            )
            return

        # 8. 답변 전송 (Block Kit)
        context_count = len(contexts)
        
        # 같은 주제의 과거 Q&A 조회
        related_qa = []
        if topic and topic != "미분류":
            from db.repository import get_recent_qa_by_topic
            related_qa = get_recent_qa_by_topic(session, channel_id, topic, limit=3)
            
        sent_ts = post_answer(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            answer=answer,
            context_count=context_count,
            thinking_ts=thinking_ts,
            show_thread_tip=show_thread_tip,
            related_qa=related_qa,
        )

        # 9. 피드백 이모지 시드 추가 (reactions:write 스코프 필요)
        if sent_ts:
            from ui.reaction_handler import add_feedback_reactions
            add_feedback_reactions(client=client, channel=channel_id, message_ts=sent_ts)

        MESSAGE_PROCESSED_TOTAL.labels(status="success").inc()

        # 10. 봇 응답 저장 (응답 시간·입출력 토큰·RAG·웹검색 메타 함께 기록)
        _elapsed_ms = int((time.monotonic() - _start_time) * 1000)
        _save_message_and_embed(
            session_factory=session_factory,
            event_id=None,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=sent_ts or f"bot_{message_ts}",
            user_id=None,
            role="bot",
            content=answer,
            is_question=False,
            response_time_ms=_elapsed_ms,
            prompt_tokens=_prompt_tokens,
            completion_tokens=estimate_tokens(answer),
            rag_avg_similarity=_rag_avg_sim,
            used_web_search=bool(web_search_block),
        )

    except Exception as exc:
        logger.error(f"질문 처리 중 오류: {exc}", exc_info=True)
        MESSAGE_PROCESSED_TOTAL.labels(status="error").inc()
        _send_error_or_fallback(
            client=client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            question=question,
            thinking_ts=thinking_ts,
        )
    finally:
        session.close()


def _delete_thinking_msg(client, channel_id: str, thinking_ts: Optional[str]) -> None:
    """임시 '답변 생성 중' 메시지를 삭제한다."""
    if thinking_ts:
        try:
            client.chat_delete(channel=channel_id, ts=thinking_ts)
        except Exception:
            pass


def _send_error_or_fallback(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    question: str,
    thinking_ts: Optional[str],
) -> None:
    """오류 발생 시 Block Kit 에러 메시지를 전송하거나 thinking 메시지를 업데이트한다."""
    post_error(
        client=client,
        channel=channel_id,
        thread_ts=thread_ts,
        thinking_ts=thinking_ts,
    )


# ---------------------------------------------------------------------------
# Bolt 이벤트 핸들러 등록
# ---------------------------------------------------------------------------

def register_handlers(app: App, session_factory, bot_user_id: Optional[str] = None) -> None:
    """
    Bolt 앱에 이벤트 핸들러를 등록한다.
    session_factory는 스레드 안전한 scoped_session이어야 한다.
    bot_user_id는 main.py에서 auth_test()로 획득해 전달한다.
    """
    # 제품 카테고리 기본값 시딩 (테이블이 비어 있는 경우에만)
    try:
        _seed_session = session_factory()
        seed_product_categories(_seed_session)
        _seed_session.commit()
        _seed_session.close()
    except Exception as _seed_err:
        logger.warning(f"제품 카테고리 시딩 실패: {_seed_err}")

    @app.event("app_mention")
    def handle_mention(event, client, ack, say):
        """@챗봇 멘션 이벤트를 처리한다. 즉시 ack 후 스레드로 분리한다."""
        ack()

        channel_id = event.get("channel")
        is_new_thread = event.get("thread_ts") is None  # 기존 스레드 없이 채널에서 새로 시작
        thread_ts = event.get("thread_ts") or event.get("ts")
        message_ts = event.get("ts", "")
        # app_mention과 message 이벤트는 동일한 event_ts를 가지므로 prefix로 구분한다.
        event_id = "mention_" + (event.get("event_ts") or message_ts)
        user_id = event.get("user")
        raw_text = event.get("text", "")

        # Socket Mode는 2개 연결을 유지하므로 동일 이벤트가 두 번 전달될 수 있다.
        if _is_duplicate_event(event_id):
            logger.debug(f"중복 이벤트 무시: event_id={event_id}")
            return

        # 멘션 태그 제거
        question = _clean_mention_text(raw_text, bot_user_id or "")

        if not question:
            send_greeting_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
            )
            return

        # ── 소개 / 도움말 명령 감지 ─────────────────────────────────────────
        if _is_intro_command(question):
            logger.info(f"소개 명령 수신: user={user_id}")
            payload = build_intro_blocks()
            post_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
                text=payload["text"],
                blocks=payload["blocks"],
            )
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 백필 명령 감지 ──────────────────────────────────────────────────
        is_backfill, backfill_days = _is_backfill_command(question)
        if is_backfill:
            # 권한 확인: BACKFILL_ADMIN_USER_IDS가 설정된 경우 해당 사용자만 허용
            if config.BACKFILL_ADMIN_USER_IDS and user_id not in config.BACKFILL_ADMIN_USER_IDS:
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="⛔ 백필 실행 권한이 없습니다. 관리자에게 문의하세요.",
                )
                return
            logger.info(f"백필 명령 수신: user={user_id} days={backfill_days}")
            _run_backfill_in_background(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                days=backfill_days,
                session_factory=session_factory,
            )
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 히스토리 명령 감지 ──────────────────────────────────────────────
        is_history, history_days = _is_history_command(question)
        if is_history:
            logger.info(f"히스토리 명령 수신: user={user_id} channel={channel_id} days={history_days}")

            def history_worker():
                hist_session = session_factory()
                try:
                    topic_groups, total_count = get_channel_history_by_topic(
                        hist_session, channel_id, days=history_days
                    )
                    channel_label = f"<#{channel_id}>"
                    if topic_groups:
                        payload = build_history_grouped_blocks(
                            topic_groups, total_count, channel_label, days=history_days
                        )
                    else:
                        # 질문이 아예 없으면 날짜별 뷰로 폴백 (빈 상태 메시지 표시)
                        grouped = get_channel_history_by_date(hist_session, channel_id, days=history_days)
                        payload = build_history_blocks(grouped, channel_label, days=history_days)
                    post_message(
                        client=client,
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=payload["text"],
                        blocks=payload["blocks"],
                    )
                finally:
                    hist_session.close()

            threading.Thread(target=history_worker, daemon=True).start()
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 대시보드 명령 감지 ──────────────────────────────────────────────
        is_dashboard, dashboard_days = _is_dashboard_command(question)
        if is_dashboard:
            logger.info(f"대시보드 명령 수신: user={user_id} days={dashboard_days}")

            def dashboard_worker():
                dash_session = session_factory()
                try:
                    stats = get_dashboard_stats(dash_session, period_days=dashboard_days)
                    fallbacks = get_recent_fallbacks(dash_session, period_days=dashboard_days, limit=5)
                    top_topics = get_top_topics(dash_session, period_days=dashboard_days, limit=5)
                    payload = build_dashboard_blocks(
                        stats,
                        fallback_questions=fallbacks,
                        top_topics=top_topics,
                    )
                    post_message(
                        client=client,
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=payload["text"],
                        blocks=payload["blocks"],
                    )
                finally:
                    dash_session.close()

            threading.Thread(target=dashboard_worker, daemon=True).start()
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 알림 관리자 명령 감지 ────────────────────────────────────────────
        if _handle_notification_admin_command(client, channel_id, thread_ts, question, session_factory):
            logger.info(f"알림 관리자 명령 처리 완료: user={user_id}")
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 제품 후보 명령 감지 (B방향 동적 발견) ───────────────────────────
        if _parse_product_candidate_command(question)[0] != "none":
            logger.info(f"제품 후보 명령 수신: user={user_id}")
            threading.Thread(
                target=_handle_product_candidate_command,
                args=(client, channel_id, thread_ts, question, session_factory),
                daemon=True,
            ).start()
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 담당자 관리 명령 감지 ────────────────────────────────────────────
        if _parse_owner_command(question)[0] != "none":
            logger.info(f"담당자 관리 명령 수신: user={user_id}")
            threading.Thread(
                target=_handle_owner_command,
                args=(client, channel_id, thread_ts, question, session_factory),
                daemon=True,
            ).start()
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 요약 주기 명령 감지 ─────────────────────────────────────────────
        is_schedule, is_view_only = _is_schedule_command(question)
        if is_schedule:
            if is_view_only:
                logger.info(f"요약 주기 확인 명령 수신: user={user_id}")
                from batch.scheduler import get_current_schedule_description
                desc = get_current_schedule_description(session_factory)
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"🕐 현재 요약 배치 주기: *{desc}*",
                )
            else:
                # "요약 주기 설정 매일 3시" 에서 설정 부분 추출
                lower_q = question.strip().lower()
                rest = question.strip()
                for kw in _SCHEDULE_SET_KEYWORDS:
                    if lower_q.startswith(kw.lower()):
                        rest = question.strip()[len(kw):].strip()
                        break

                cfg = _parse_schedule_config(rest)
                if cfg is None:
                    post_message(
                        client=client,
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=(
                            "⚠️ 주기 형식을 인식하지 못했습니다.\n"
                            "다음 형식으로 입력해 주세요.\n"
                            "• `요약 주기 설정 매일 3시`\n"
                            "• `요약 주기 설정 매주 월요일 2시`\n"
                            "• `요약 주기 설정 매월 1일 2시`"
                        ),
                    )
                else:
                    logger.info(f"요약 주기 변경 명령 수신: user={user_id} cfg={cfg}")
                    from batch.scheduler import update_summary_schedule
                    desc = update_summary_schedule(session_factory, cfg)
                    post_message(
                        client=client,
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=f"✅ 요약 배치 주기가 변경되었습니다. *{desc}* 기준으로 실행됩니다.",
                    )
            return
        # ────────────────────────────────────────────────────────────────────

        # ── 정규화 명령 감지 ────────────────────────────────────────────────
        if _is_normalize_command(question):
            if config.BACKFILL_ADMIN_USER_IDS and user_id not in config.BACKFILL_ADMIN_USER_IDS:
                post_message(
                    client=client,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="⛔ 정규화 실행 권한이 없습니다. 관리자에게 문의하세요.",
                )
                return
            logger.info(f"정규화 명령 수신: user={user_id}")
            _run_normalize_in_background(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_factory=session_factory,
            )
            return
        # ────────────────────────────────────────────────────────────────────

        logger.info(f"앱 멘션 수신: channel={channel_id} user={user_id} text={question[:50]!r}")

        # 즉시 '답변 중' 표시 전송 (3초 이내 ack 이후)
        thinking_ts = post_thinking_indicator(client=client, channel=channel_id, thread_ts=thread_ts)

        def worker():
            from utils.token_counter import estimate_tokens
            user_name = get_user_display_name(client, user_id) if user_id else "익명"

            # 첨부 이미지가 있으면 압축 후 vision 모델로 분석 (최대 _MAX_IMAGES개)
            effective_question = question
            image_context = _build_image_context(event, config.SLACK_BOT_TOKEN)
            if image_context:
                effective_question = (
                    f"[첨부 이미지 분석]\n{image_context}\n\n{question}".strip()
                )

            # 제품 힌트 조회 + 주제/제품 LLM 분류 (단일 호출)
            _prod_session = session_factory()
            try:
                _products = get_products_as_llm_hints(_prod_session)
            finally:
                _prod_session.close()
            _topic, _product_key = extract_topic_and_product(effective_question, _products)

            _save_message_and_embed(
                session_factory=session_factory,
                event_id=event_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                user_id=user_id,
                role="user",
                content=effective_question,
                is_question=True,
                prompt_tokens=estimate_tokens(effective_question),
                topic=_topic,
                product_key=_product_key,
            )

            # 제품 질문 카운트 증가 + 담당자 미지정 시 관리자 알림
            if _product_key:
                _cnt_session = session_factory()
                try:
                    increment_product_question_count(_cnt_session, _product_key)
                    _cnt_session.commit()
                finally:
                    _cnt_session.close()
                threading.Thread(
                    target=_notify_admin_unowned_products,
                    args=(client, session_factory),
                    daemon=True,
                ).start()

            # 스레드 문맥 조회 및 요약
            thread_summary = None
            if thread_ts:
                thread_msgs = fetch_thread_history(client, channel_id, thread_ts, limit=20)
                # 현재 메시지는 스레드 요약에서 제외 (마지막 메시지 제외)
                if thread_msgs and len(thread_msgs) >= 3:
                    thread_summary = summarize_thread_context(thread_msgs[:-1])

            _process_question(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                question=effective_question,
                user_id=user_id,
                user_name=user_name,
                session_factory=session_factory,
                thinking_ts=thinking_ts,
                thread_summary=thread_summary,
                image_context=image_context or None,
                show_thread_tip=is_new_thread,
                product_key=_product_key,
                topic=_topic,
            )

        threading.Thread(target=worker, daemon=True).start()

    @app.event("message")
    def handle_message(event, client, ack):
        """
        채널 및 DM 메시지 이벤트를 처리한다.
        - 봇 메시지, subtype 이벤트(편집/삭제)는 무시한다.
        - 채널: TARGET_CHANNEL_IDS에 포함된 채널만 처리한다.
        - DM(channel_type=im): ENABLE_DM_HANDLER=true이면 모든 메시지를 처리한다.
        """
        ack()

        # 봇 자신의 메시지 또는 메시지 수정/삭제 이벤트 필터링
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")
        if subtype in ("bot_message", "message_changed", "message_deleted") or bot_id:
            return

        channel_id = event.get("channel")
        if not channel_id:
            return

        channel_type = event.get("channel_type", "")
        is_dm = channel_type == "im"

        # DM이면 ENABLE_DM_HANDLER 확인, 채널이면 TARGET_CHANNEL_IDS 필터 적용
        if is_dm:
            if not config.ENABLE_DM_HANDLER:
                return
        else:
            if config.TARGET_CHANNEL_IDS and channel_id not in config.TARGET_CHANNEL_IDS:
                return

        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts", "")
        event_id = "msg_" + (event.get("event_ts") or message_ts)
        user_id = event.get("user")
        raw_text = event.get("text", "")

        if _is_duplicate_event(event_id):
            logger.debug(f"중복 메시지 이벤트 무시: event_id={event_id}")
            return

        if not raw_text.strip() and not event.get("files"):
            return

        is_mention_event = bot_user_id and f"<@{bot_user_id}>" in raw_text

        logger.debug(f"메시지 수신: channel={channel_id} type={channel_type} user={user_id} text={raw_text[:50]!r}")

        def worker():
            image_ctx = analyze_slack_files(event.get("files") or [], config.SLACK_BOT_TOKEN)
            if image_ctx:
                effective_content = (
                    f"[첨부 이미지 분석]\n{image_ctx}\n\n{raw_text}".strip()
                    if raw_text.strip()
                    else f"[첨부 이미지 분석]\n{image_ctx}"
                )
            else:
                effective_content = raw_text

            # --- DM 전용 처리 ---
            # DM은 멘션·분류기·스레드 작성자 체크 없이 모든 메시지를 질문으로 처리한다.
            # RAG 검색은 전체 채널 대상으로 수행한다 (rag_channel_id=None).
            if is_dm:
                _dm_prod_session = session_factory()
                try:
                    _dm_products = get_products_as_llm_hints(_dm_prod_session)
                finally:
                    _dm_prod_session.close()
                _dm_topic, _dm_product_key = extract_topic_and_product(effective_content, _dm_products)

                _save_message_and_embed(
                    session_factory=session_factory,
                    event_id=event_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    message_ts=message_ts,
                    user_id=user_id,
                    role="user",
                    content=effective_content,
                    is_question=True,
                    topic=_dm_topic,
                    product_key=_dm_product_key,
                )

                user_name = get_user_display_name(client, user_id) if user_id else "익명"
                thinking_ts = post_thinking_indicator(
                    client=client, channel=channel_id, thread_ts=None
                )

                if _dm_product_key:
                    _dm_cnt_session = session_factory()
                    try:
                        increment_product_question_count(_dm_cnt_session, _dm_product_key)
                        _dm_cnt_session.commit()
                    finally:
                        _dm_cnt_session.close()
                    threading.Thread(
                        target=_notify_admin_unowned_products,
                        args=(client, session_factory),
                        daemon=True,
                    ).start()

                _process_question(
                    client=client,
                    channel_id=channel_id,
                    thread_ts=None,
                    message_ts=message_ts,
                    question=effective_content,
                    user_id=user_id,
                    user_name=user_name,
                    session_factory=session_factory,
                    thinking_ts=thinking_ts,
                    rag_channel_id=None,
                    image_context=image_ctx or None,
                    product_key=_dm_product_key,
                    topic=_dm_topic,
                )
                return

            # --- 채널 처리 ---

            # 멘션 메시지: 저장만 하고 답변은 app_mention 핸들러에 위임
            if is_mention_event:
                _save_message_and_embed(
                    session_factory=session_factory,
                    event_id=event_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    message_ts=message_ts,
                    user_id=user_id,
                    role="user",
                    content=raw_text,
                    is_question=True,
                )
                return

            # 가드 1: 다른 사용자 멘션이 있고 봇 멘션이 없으면 저장만
            if _has_user_mention(raw_text) and not is_mention_event:
                logger.info(
                    f"타인 멘션 메시지 감지 — 분류 생략, 저장만 처리: "
                    f"channel={channel_id} user={user_id} text={raw_text[:50]!r}"
                )
                _save_message_and_embed(
                    session_factory=session_factory,
                    event_id=event_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    message_ts=message_ts,
                    user_id=user_id,
                    role="user",
                    content=effective_content,
                    is_question=False,
                )
                return

            # 가드 2: 스레드 원글 작성자가 아닌 사람의 답글이면 저장만
            if thread_ts:
                _guard_session = session_factory()
                try:
                    starter_id = get_thread_starter_user_id(
                        _guard_session, channel_id, thread_ts
                    )
                finally:
                    _guard_session.close()
                if starter_id and starter_id != user_id:
                    logger.info(
                        f"스레드 원글 작성자({starter_id})와 다른 사용자({user_id})의 "
                        f"답글 — 분류 생략, 저장만 처리"
                    )
                    _save_message_and_embed(
                        session_factory=session_factory,
                        event_id=event_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        message_ts=message_ts,
                        user_id=user_id,
                        role="user",
                        content=effective_content,
                        is_question=False,
                    )
                    return

            # 채널 메시지는 저장만 수행한다. 응답은 app_mention(@멘션)에서만 처리한다.
            # 분류기는 is_question 플래그 품질 유지를 위해 실행한다.
            classify_result = classify_message(
                message=raw_text,
                is_mention=False,
                bot_user_id=bot_user_id,
                sender_user_id=user_id,
            )

            from utils.token_counter import estimate_tokens
            _topic, _product_key = None, None
            if classify_result.is_actionable:
                _ch_prod_session = session_factory()
                try:
                    _ch_products = get_products_as_llm_hints(_ch_prod_session)
                finally:
                    _ch_prod_session.close()
                _topic, _product_key = extract_topic_and_product(effective_content, _ch_products)
            if not classify_result.is_actionable:
                MESSAGE_PROCESSED_TOTAL.labels(status="ignored").inc()

            _save_message_and_embed(
                session_factory=session_factory,
                event_id=event_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                user_id=user_id,
                role="user",
                content=effective_content,
                is_question=classify_result.is_actionable,
                prompt_tokens=estimate_tokens(effective_content),
                topic=_topic,
                product_key=_product_key,
            )
            if _product_key:
                _ch_cnt_session = session_factory()
                try:
                    increment_product_question_count(_ch_cnt_session, _product_key)
                    _ch_cnt_session.commit()
                finally:
                    _ch_cnt_session.close()
                threading.Thread(
                    target=_notify_admin_unowned_products,
                    args=(client, session_factory),
                    daemon=True,
                ).start()

        threading.Thread(target=worker, daemon=True).start()
