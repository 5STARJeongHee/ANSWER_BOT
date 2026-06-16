# Slack 메시지 전송 및 대화 이력 조회 서비스
from __future__ import annotations
import logging
import time
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import config

logger = logging.getLogger(__name__)


def _with_retry(func, *args, max_retries: int = 3, **kwargs):
    """Slack API 호출에 지수 백오프 재시도를 적용한다."""
    import random
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except SlackApiError as exc:
            error_code = exc.response.get("error", "")
            if error_code == "ratelimited":
                retry_after = int(exc.response.headers.get("Retry-After", 1))
                logger.warning(f"Slack rate limit, {retry_after}초 후 재시도")
                time.sleep(retry_after)
            elif attempt == max_retries - 1:
                raise
            else:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Slack API 오류 재시도 ({attempt + 1}/{max_retries}): {exc}")
                time.sleep(wait)
    return None


def post_message(
    client: WebClient,
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
) -> Optional[dict]:
    """Slack 채널 또는 스레드에 메시지를 전송한다."""
    kwargs: dict = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    try:
        response = _with_retry(client.chat_postMessage, **kwargs)
        return response
    except SlackApiError as exc:
        logger.error(f"메시지 전송 실패 (channel={channel}): {exc}")
        return None


def post_thinking_indicator(
    client: WebClient,
    channel: str,
    thread_ts: Optional[str] = None,
) -> Optional[str]:
    """
    '답변 생성 중...' 임시 메시지를 전송하고 메시지 ts를 반환한다.
    나중에 update_message()로 교체한다.
    """
    response = post_message(
        client=client,
        channel=channel,
        text="답변을 생성 중입니다... :hourglass_flowing_sand:",
        thread_ts=thread_ts,
    )
    if response and response.get("ok"):
        return response["ts"]
    return None


def update_message(
    client: WebClient,
    channel: str,
    ts: str,
    text: str,
) -> bool:
    """기존 메시지를 새 내용으로 업데이트한다."""
    try:
        _with_retry(client.chat_update, channel=channel, ts=ts, text=text)
        return True
    except SlackApiError as exc:
        logger.error(f"메시지 업데이트 실패 (channel={channel}, ts={ts}): {exc}")
        return False


def fetch_channel_history(
    client: WebClient,
    channel_id: str,
    oldest: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """
    채널의 메시지 이력을 페이지네이션으로 수집한다.
    Slack API rate limit(50회/분) 대응을 위해 호출 간격을 조절한다.
    """
    messages = []
    cursor = None

    while True:
        kwargs: dict = {"channel": channel_id, "limit": min(limit, 200)}
        if oldest:
            kwargs["oldest"] = oldest
        if cursor:
            kwargs["cursor"] = cursor

        try:
            response = _with_retry(client.conversations_history, **kwargs)
            if not response or not response.get("ok"):
                break

            batch = response.get("messages", [])
            messages.extend(batch)

            # 페이지네이션
            meta = response.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor or len(messages) >= limit:
                break

            # Rate limit 대응: 1.2초 대기 (50회/분 = 1.2초/회)
            time.sleep(1.2)

        except SlackApiError as exc:
            logger.error(f"이력 조회 실패 (channel={channel_id}): {exc}")
            break

    return messages[:limit]


def get_user_info(client: WebClient, user_id: str) -> Optional[dict]:
    """Slack 사용자 정보를 조회한다."""
    try:
        response = _with_retry(client.users_info, user=user_id)
        if response and response.get("ok"):
            return response.get("user")
    except SlackApiError as exc:
        logger.warning(f"사용자 정보 조회 실패 (user_id={user_id}): {exc}")
    return None


def get_user_display_name(client: WebClient, user_id: str) -> str:
    """사용자 표시 이름을 반환한다. 조회 실패 시 user_id를 반환한다."""
    user = get_user_info(client, user_id)
    if user:
        profile = user.get("profile", {})
        return profile.get("display_name") or profile.get("real_name") or user_id
    return user_id


def build_fallback_mention(fallback_user_ids: list[str]) -> str:
    """담당자 멘션 문자열을 생성한다."""
    if not fallback_user_ids:
        return "담당자"
    mentions = " ".join(f"<@{uid}>" for uid in fallback_user_ids)
    return mentions


def send_fallback_message(
    client: WebClient,
    channel: str,
    thread_ts: Optional[str],
    question: str,
    fallback_user_ids: Optional[list[str]] = None,
) -> None:
    """
    챗봇이 답변하기 어려운 경우 담당자 호출 메시지를 전송한다.
    fallback 이력은 호출자가 DB에 기록한다.
    """
    if fallback_user_ids is None:
        fallback_user_ids = config.FALLBACK_MENTION_USER_IDS

    mention_str = build_fallback_mention(fallback_user_ids)
    text = (
        f"안녕하세요! 해당 질문에 대해 정확한 답변을 드리기 어렵습니다. :bow:\n\n"
        f"더 정확한 정보를 위해 담당자 {mention_str}에게 문의해 주세요.\n\n"
        f"> 질문: {question[:200]}"
    )
    post_message(client=client, channel=channel, text=text, thread_ts=thread_ts)
    logger.info(f"Fallback 메시지 전송 완료 (channel={channel})")
