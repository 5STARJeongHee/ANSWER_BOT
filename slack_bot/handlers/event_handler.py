# Slack Bolt 이벤트 핸들러 - app_mention 및 message 이벤트 처리
from __future__ import annotations
import logging
import threading
from typing import Optional

from slack_bolt import App

import config
from db.models import get_session_factory
from db.repository import upsert_message
from services.classifier import classify_message, MessageCategory
from services.context_retriever import retrieve_context, format_context_for_prompt, embed_text
from services.llm_service import call_qa, parse_json_response
from services.slack_service import (
    post_thinking_indicator,
    update_message,
    post_message,
    send_fallback_message,
    get_user_display_name,
)
from utils.pii_filter import apply_pii_filter, has_pii
from utils.token_counter import trim_messages_to_budget

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# QA 프롬프트 상수
# ---------------------------------------------------------------------------
_QA_SYSTEM_PROMPT = (
    "너는 사내 업무 지원 Slack 챗봇이다. 정확하고 간결하게 한국어로 답변하라.\n"
    "모르는 내용은 추측하지 말고 '확인이 필요합니다'라고 답하라.\n"
    "답변은 2~5문장 내로 핵심만 전달하라.\n"
    "AI 생성 답변임을 사용자가 인지할 수 있도록 답변 끝에 '[AI 생성 답변]'을 덧붙인다."
)

_FALLBACK_EVAL_PROMPT = (
    "아래 답변 초안이 사용자의 질문에 대해 충분히 신뢰할 수 있는 답변인지 평가하라.\n"
    "평가 기준:\n"
    "- 사실 확인이 필요한 정책/수치/일정 정보를 포함하는가\n"
    "- 컨텍스트에 근거 없이 추측한 부분이 있는가\n"
    '출력(JSON): {"can_answer_directly": true/false, "fallback_message": "담당자 호출용 안내 문구"}'
)


# ---------------------------------------------------------------------------
# 보조 함수
# ---------------------------------------------------------------------------

def _clean_mention_text(text: str, bot_user_id: str) -> str:
    """@봇 멘션 텍스트에서 멘션 태그를 제거한다."""
    return text.replace(f"<@{bot_user_id}>", "").strip()


def _evaluate_answer(question: str, draft_answer: str) -> bool:
    """
    LLM으로 답변 신뢰도를 평가하고 직접 답변 가능 여부를 반환한다.
    평가 실패 시 True를 반환하여 답변 전송을 기본 동작으로 유지한다.
    """
    from services.llm_service import call_with_fallback
    messages = [
        {"role": "system", "content": _FALLBACK_EVAL_PROMPT},
        {
            "role": "user",
            "content": f"질문: {question}\n답변 초안: {draft_answer}",
        },
    ]
    raw = call_with_fallback(
        model_chain=config.CLASSIFIER_FALLBACK_CHAIN,
        messages=messages,
        max_tokens=100,
        response_format={"type": "json_object"},
    )
    parsed = parse_json_response(
        raw or "", default={"can_answer_directly": True}
    )
    return bool(parsed.get("can_answer_directly", True))


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

    # 2단계: 임베딩 생성 후 저장 (CPU 집약 작업은 커밋 후 수행)
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
        # 임베딩 실패는 메시지 저장 성공에 영향을 주지 않는다.
    finally:
        session2.close()

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
) -> None:
    """
    LLM으로 질문에 답변을 생성하고 Slack에 전송한다.
    threading.Thread 내에서 실행되므로 독립 세션을 사용한다.
    """
    session = session_factory()
    try:
        # 1. RAG 컨텍스트 검색
        contexts = retrieve_context(
            session=session,
            question=question,
            channel_id=channel_id,
        )
        context_text = format_context_for_prompt(contexts)

        # 2. 최근 메시지 조회
        from db.repository import get_recent_messages
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

        # 3. QA 프롬프트 구성
        user_message_content = (
            f"[참고 컨텍스트 - 과거 관련 대화]\n{context_text}\n\n"
            f"[최근 대화 이력]\n{recent_text or '(없음)'}\n\n"
            f"[현재 질문]\n작성자: {user_name}\n내용: {question}"
        )
        messages = [
            {"role": "system", "content": _QA_SYSTEM_PROMPT},
            {"role": "user", "content": user_message_content},
        ]

        # 4. 토큰 예산 내로 메시지 정리
        messages_trimmed = trim_messages_to_budget(
            messages=messages,
            system_prompt=_QA_SYSTEM_PROMPT,
            max_tokens=config.MAX_CONTEXT_TOKENS,
        )

        # 5. 답변 생성
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

        # 6. Fallback 평가
        can_answer = _evaluate_answer(question, answer)
        if not can_answer:
            logger.info(f"Fallback 판단: 답변 불확실 (question={question[:50]!r})")
            _delete_thinking_msg(client, channel_id, thinking_ts)
            send_fallback_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
                question=question,
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

        # 7. 답변 전송
        if thinking_ts:
            update_message(client=client, channel=channel_id, ts=thinking_ts, text=answer)
        else:
            post_message(client=client, channel=channel_id, text=answer, thread_ts=thread_ts)

        # 8. 봇 응답 저장
        _save_message_and_embed(
            session_factory=session_factory,
            event_id=None,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=f"bot_{message_ts}",
            user_id=None,
            role="bot",
            content=answer,
            is_question=False,
        )

    except Exception as exc:
        logger.error(f"질문 처리 중 오류: {exc}", exc_info=True)
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
    """오류 발생 시 에러 메시지 또는 fallback을 전송한다."""
    if thinking_ts:
        update_message(
            client=client,
            channel=channel_id,
            ts=thinking_ts,
            text="죄송합니다, 현재 답변을 생성할 수 없습니다. 잠시 후 다시 시도해 주세요. :pray:",
        )
    else:
        send_fallback_message(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            question=question,
        )


# ---------------------------------------------------------------------------
# Bolt 이벤트 핸들러 등록
# ---------------------------------------------------------------------------

def register_handlers(app: App, session_factory) -> None:
    """
    Bolt 앱에 이벤트 핸들러를 등록한다.
    session_factory는 스레드 안전한 scoped_session이어야 한다.
    """
    # auth_test는 시작 시 1회만 호출하여 매 이벤트마다 API 왕복을 방지한다.
    try:
        bot_user_id: Optional[str] = app.client.auth_test()["user_id"]
        logger.info(f"봇 user_id 확인 완료: {bot_user_id}")
    except Exception as exc:
        logger.warning(f"auth_test 실패, bot_user_id 없이 동작합니다: {exc}")
        bot_user_id = None

    @app.event("app_mention")
    def handle_mention(event, client, ack, say):
        """@챗봇 멘션 이벤트를 처리한다. 즉시 ack 후 스레드로 분리한다."""
        ack()

        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        message_ts = event.get("ts", "")
        event_id = event.get("event_ts") or message_ts
        user_id = event.get("user")
        raw_text = event.get("text", "")

        # 멘션 태그 제거
        question = _clean_mention_text(raw_text, bot_user_id or "")

        if not question:
            say(text="안녕하세요! 무엇을 도와드릴까요? :wave:", thread_ts=thread_ts)
            return

        # 중복 이벤트 조회 (DB 저장 전 사전 체크)
        # 실제 DB 중복 방지는 upsert_message의 unique constraint이 보장
        logger.info(f"앱 멘션 수신: channel={channel_id} user={user_id} text={question[:50]!r}")

        # 즉시 '답변 중' 표시 전송 (3초 이내 ack 이후)
        thinking_ts = post_thinking_indicator(client=client, channel=channel_id, thread_ts=thread_ts)

        # 사용자 메시지 저장 (비동기 스레드에서 처리)
        def worker():
            user_name = get_user_display_name(client, user_id) if user_id else "익명"

            _save_message_and_embed(
                session_factory=session_factory,
                event_id=event_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                user_id=user_id,
                role="user",
                content=question,
                is_question=True,
            )

            _process_question(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                question=question,
                user_id=user_id,
                user_name=user_name,
                session_factory=session_factory,
                thinking_ts=thinking_ts,
            )

        threading.Thread(target=worker, daemon=True).start()

    @app.event("message")
    def handle_message(event, client, ack):
        """
        채널 메시지 이벤트를 처리한다.
        - 봇 메시지, subtype 이벤트(편집/삭제)는 무시한다.
        - 대상 채널의 메시지만 수집 및 분류한다.
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

        # 지정 채널만 처리
        if config.TARGET_CHANNEL_IDS and channel_id not in config.TARGET_CHANNEL_IDS:
            return

        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts", "")
        event_id = event.get("event_ts") or message_ts
        user_id = event.get("user")
        raw_text = event.get("text", "")

        if not raw_text or not raw_text.strip():
            return

        # 봇 멘션이 포함된 메시지는 app_mention 핸들러가 처리하므로 여기서는 저장만 한다.
        # 답변 분기를 실행하면 이중 답변이 발생한다.
        is_mention_event = bot_user_id and f"<@{bot_user_id}>" in raw_text

        logger.debug(f"메시지 수신: channel={channel_id} user={user_id} text={raw_text[:50]!r}")

        def worker():
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

            # 분류기 실행
            classify_result = classify_message(
                message=raw_text,
                is_mention=False,
                bot_user_id=bot_user_id,
                sender_user_id=user_id,
            )

            # 메시지 저장 (질문 여부 포함)
            _save_message_and_embed(
                session_factory=session_factory,
                event_id=event_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                user_id=user_id,
                role="user",
                content=raw_text,
                is_question=classify_result.is_actionable,
            )

            # 질문/요청인 경우만 답변 생성
            if not classify_result.is_actionable:
                return

            user_name = get_user_display_name(client, user_id) if user_id else "익명"
            thinking_ts = post_thinking_indicator(
                client=client, channel=channel_id, thread_ts=thread_ts or message_ts
            )

            _process_question(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts or message_ts,
                message_ts=message_ts,
                question=raw_text,
                user_id=user_id,
                user_name=user_name,
                session_factory=session_factory,
                thinking_ts=thinking_ts,
            )

        threading.Thread(target=worker, daemon=True).start()
