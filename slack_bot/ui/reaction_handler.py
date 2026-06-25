# 👍/👎 이모지 리액션으로 답변 품질 피드백을 수집하는 핸들러
from __future__ import annotations

import logging
import threading
from typing import Optional

from slack_bolt import App
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# 피드백으로 집계할 이모지 이름 (Slack에서 ':thumbsup:' → 'thumbsup')
_POSITIVE_REACTIONS = frozenset({"thumbsup", "+1", "white_check_mark"})
_NEGATIVE_REACTIONS = frozenset({"thumbsdown", "-1", "x"})

# 봇 답변에 자동으로 추가할 시드 이모지
_SEED_REACTIONS = ("thumbsup", "thumbsdown")

# 부정 피드백 투표 버튼 구성 (action_id, reason_value, 표시 라벨)
_VOTE_BUTTONS = [
    ("fb_wrong_source", "wrong_source", "정보가 틀렸어요"),
    ("fb_hallucination", "hallucination", "없는 내용을 지어냈어요"),
    ("fb_out_of_scope", "out_of_scope", "모르는 주제예요"),
    ("fb_format_issue", "format_issue", "표현이 어색해요"),
]


def add_feedback_reactions(
    client,
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


def _handle_positive_feedback(
    session_factory,
    channel: str,
    message_ts: str,
) -> None:
    """긍정 피드백: 봇 답변과 원 질문을 QA 쌍으로 RAG에 임베딩한다."""
    from db.repository import get_bot_message_with_question, save_qa_feedback_embedding

    session = session_factory()
    try:
        bot_msg, question_msg = get_bot_message_with_question(session, channel, message_ts)
    finally:
        session.close()

    if not bot_msg:
        logger.debug(f"긍정 피드백: 봇 메시지를 찾지 못함 (channel={channel} ts={message_ts})")
        return
    if not question_msg:
        logger.debug(f"긍정 피드백: 원 질문을 찾지 못함 (bot_msg_id={bot_msg.id})")
        return

    ok = save_qa_feedback_embedding(
        session_factory,
        bot_msg_id=bot_msg.id,
        question_text=question_msg.content,
        answer_text=bot_msg.content,
    )
    if ok:
        logger.info(f"긍정 피드백 QA 임베딩 저장 완료 (bot_msg_id={bot_msg.id})")


def _handle_negative_feedback(
    session_factory,
    client,
    channel: str,
    message_ts: str,
    user_id: str,
) -> None:
    """
    부정 피드백.
    1. LLM으로 실패 원인 분류 후 DB 저장.
    2. 사용자에게 4가지 선택 버튼이 담긴 ephemeral 메시지 발송.
    """
    from db.repository import get_bot_message_with_question, update_feedback_failure_reason
    from services.llm_service import call_feedback_classifier

    session = session_factory()
    try:
        bot_msg, question_msg = get_bot_message_with_question(session, channel, message_ts)
        thread_ts = bot_msg.thread_ts if bot_msg else None
    finally:
        session.close()

    # LLM 분류
    if bot_msg and question_msg:
        reason = call_feedback_classifier(question_msg.content, bot_msg.content)
        if reason != "unknown":
            session2 = session_factory()
            try:
                update_feedback_failure_reason(
                    session2,
                    message_ts=message_ts,
                    user_id=user_id,
                    llm_reason=reason,
                )
                session2.commit()
            except Exception as exc:
                session2.rollback()
                logger.warning(f"LLM 실패 원인 저장 실패: {exc}")
            finally:
                session2.close()
            logger.info(f"부정 피드백 LLM 분류 저장 완료: reason={reason} ts={message_ts}")

    # ephemeral 투표 메시지 발송 (👎 누른 사용자에게만)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "답변이 도움이 되지 않으셨군요. 어떤 부분이 문제였나요?",
            },
        },
        {
            "type": "actions",
            "block_id": "feedback_vote",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label},
                    "value": message_ts,
                    "action_id": action_id,
                }
                for action_id, _, label in _VOTE_BUTTONS
            ],
        },
    ]
    try:
        client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            thread_ts=thread_ts or message_ts,
            blocks=blocks,
            text="답변 품질 평가 요청",
        )
        logger.info(f"부정 피드백 투표 메시지 발송 완료: channel={channel} user={user_id}")
    except SlackApiError as exc:
        logger.warning(f"ephemeral 메시지 발송 실패: {exc}")


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
            return
        finally:
            session.close()

        # 긍정/부정 후처리를 백그라운드 스레드에서 실행 (Bolt ack 이후 논블로킹)
        if is_positive:
            threading.Thread(
                target=_handle_positive_feedback,
                args=(session_factory, channel, message_ts),
                daemon=True,
                name=f"pos-feedback-{message_ts}",
            ).start()
        else:
            threading.Thread(
                target=_handle_negative_feedback,
                args=(session_factory, client, channel, message_ts, user_id),
                daemon=True,
                name=f"neg-feedback-{message_ts}",
            ).start()
