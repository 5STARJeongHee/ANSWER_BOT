# 👍/👎 이모지 리액션으로 답변 품질 피드백을 수집하는 핸들러
from __future__ import annotations

import logging
from typing import Optional

from slack_bolt import App
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# 피드백으로 집계할 이모지 이름 (Slack에서 ':thumbsup:' → 'thumbsup')
_POSITIVE_REACTIONS = frozenset({"thumbsup", "+1", "white_check_mark"})
_NEGATIVE_REACTIONS = frozenset({"thumbsdown", "-1", "x"})

# 봇 답변에 자동으로 추가할 시드 이모지
_SEED_REACTIONS = ("thumbsup", "thumbsdown")


def add_feedback_reactions(
    client: WebClient,
    channel: str,
    message_ts: str,
) -> None:
    """
    봇 답변 메시지에 👍/👎 시드 이모지를 자동으로 추가한다.
    reactions:write 스코프가 필요하다.
    이미 존재하는 이모지는 already_reacted 오류를 무시한다.
    """
    for emoji in _SEED_REACTIONS:
        try:
            client.reactions_add(
                channel=channel,
                timestamp=message_ts,
                name=emoji,
            )
        except SlackApiError as exc:
            error_code = exc.response.get("error", "")
            if error_code == "already_reacted":
                pass  # 중복 추가 무시
            else:
                logger.warning(f"시드 이모지 추가 실패 (emoji={emoji}): {exc}")


def register_reaction_handlers(app: App, session_factory, bot_user_id: Optional[str] = None) -> None:
    """reaction_added 이벤트 핸들러를 Bolt 앱에 등록한다."""

    @app.event("reaction_added")
    def handle_reaction_added(event, client, ack) -> None:
        """
        사용자가 메시지에 이모지 리액션을 추가할 때 호출된다.

        필터링 규칙.
        - 피드백 이모지(👍/👎 계열)만 처리한다.
        - 봇 자신이 추가한 시드 이모지는 무시한다 (user 필드로 판별).
        - item_user가 봇인 메시지(봇이 작성한 메시지)에 달린 리액션만 집계한다.
        """
        ack()

        reaction: str = event.get("reaction", "")
        user_id: Optional[str] = event.get("user")
        item_user: Optional[str] = event.get("item_user")
        item: dict = event.get("item", {})
        channel: Optional[str] = item.get("channel")
        message_ts: Optional[str] = item.get("ts")

        logger.info(
            f"reaction_added 수신: reaction={reaction} user={user_id} "
            f"item_user={item_user} channel={channel} ts={message_ts}"
        )

        # 봇 자신이 추가한 시드 이모지 무시
        if bot_user_id and user_id == bot_user_id:
            return

        is_positive = reaction in _POSITIVE_REACTIONS
        is_negative = reaction in _NEGATIVE_REACTIONS
        if not (is_positive or is_negative):
            return

        if not channel or not message_ts or not user_id:
            return

        # item_user가 봇인 메시지만 집계 (channels:history 스코프 불필요)
        if bot_user_id and item_user != bot_user_id:
            logger.debug(f"봇 메시지 아님, 무시: item_user={item_user} bot={bot_user_id}")
            return

        sentiment = "positive" if is_positive else "negative"

        from db.repository import save_feedback
        session = session_factory()
        try:
            result = save_feedback(
                session,
                channel_id=channel,
                message_ts=message_ts,
                user_id=user_id,
                reaction=reaction,
                sentiment=sentiment,
            )
            session.commit()
            if result:
                logger.info(
                    f"피드백 저장 완료: channel={channel} ts={message_ts} "
                    f"user={user_id} reaction={reaction} sentiment={sentiment}"
                )
        except Exception as exc:
            session.rollback()
            logger.error(f"피드백 저장 실패: {exc}", exc_info=True)
        finally:
            session.close()
