# Slack Bolt 이벤트 핸들러 - app_mention 및 message 이벤트 처리
from __future__ import annotations
import logging
import re
import threading
import time
from typing import Optional

from slack_bolt import App

import config
from db.models import get_session_factory
from db.repository import upsert_message, get_thread_starter_user_id
from services.classifier import classify_message, MessageCategory
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
from services.summarizer import summarize_thread_context
from utils.image_processor import analyze_slack_files
from utils.pii_filter import apply_pii_filter, has_pii
from utils.token_counter import trim_messages_to_budget

logger = logging.getLogger(__name__)

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
    "[참고 컨텍스트 - 과거 관련 대화] 섹션에서 [사람 답변]으로 표시된 내용을 "
    "가장 신뢰할 수 있는 근거로 우선 참고하라.\n"
    "모르는 내용은 추측하지 말고 '확인이 필요합니다'라고 답하라.\n"
    "답변은 2~5문장 내로 핵심만 전달하라.\n"
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
    "해당 결과가 질문을 직접 뒷받침하지 못할 때만 '확인이 필요합니다'라고 답하라.\n"
    "답변은 2~5문장 내로 핵심만 전달하라.\n"
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
    # 너무 짧은 메시지는 RAG 노이즈가 되므로 임베딩 생략
    if len(clean_content.strip()) < config.EMBED_MIN_CHARS:
        logger.debug(f"임베딩 생략 — 메시지 너무 짧음 ({len(clean_content)}자 < {config.EMBED_MIN_CHARS}, id={msg_id})")
        return msg_id

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
    thread_summary: Optional[str] = None,
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
            thread_summary=thread_summary,
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
            send_fallback_message(
                client=client,
                channel=channel_id,
                thread_ts=thread_ts,
                question=question,
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
        sent_ts = post_answer(
            client=client,
            channel=channel_id,
            thread_ts=thread_ts,
            answer=answer,
            context_count=context_count,
            thinking_ts=thinking_ts,
        )

        # 9. 피드백 이모지 시드 추가 (reactions:write 스코프 필요)
        if sent_ts:
            from ui.reaction_handler import add_feedback_reactions
            add_feedback_reactions(client=client, channel=channel_id, message_ts=sent_ts)

        # 10. 봇 응답 저장
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

    @app.event("app_mention")
    def handle_mention(event, client, ack, say):
        """@챗봇 멘션 이벤트를 처리한다. 즉시 ack 후 스레드로 분리한다."""
        ack()

        channel_id = event.get("channel")
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

        logger.info(f"앱 멘션 수신: channel={channel_id} user={user_id} text={question[:50]!r}")

        # 즉시 '답변 중' 표시 전송 (3초 이내 ack 이후)
        thinking_ts = post_thinking_indicator(client=client, channel=channel_id, thread_ts=thread_ts)

        # 사용자 메시지 저장 (비동기 스레드에서 처리)
        def worker():
            user_name = get_user_display_name(client, user_id) if user_id else "익명"

            # 첨부 이미지가 있으면 압축 후 vision 모델로 분석 (최대 _MAX_IMAGES개)
            effective_question = question
            image_context = _build_image_context(event, config.SLACK_BOT_TOKEN)
            if image_context:
                effective_question = (
                    f"[첨부 이미지 분석]\n{image_context}\n\n{question}".strip()
                )

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
            )

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
        # app_mention과 message 이벤트는 동일한 event_ts를 가지므로 prefix로 구분한다.
        event_id = "msg_" + (event.get("event_ts") or message_ts)
        user_id = event.get("user")
        raw_text = event.get("text", "")

        if _is_duplicate_event(event_id):
            logger.debug(f"중복 메시지 이벤트 무시: event_id={event_id}")
            return

        if not raw_text.strip() and not event.get("files"):
            return

        # 봇 멘션이 포함된 메시지는 app_mention 핸들러가 처리하므로 여기서는 저장만 한다.
        # 답변 분기를 실행하면 이중 답변이 발생한다.
        is_mention_event = bot_user_id and f"<@{bot_user_id}>" in raw_text

        logger.debug(f"메시지 수신: channel={channel_id} user={user_id} text={raw_text[:50]!r}")

        def worker():
            # 멘션 메시지: 저장만 하고 답변은 app_mention 핸들러에 위임
            # (app_mention 핸들러가 이미지 분석 포함 처리를 담당하므로 여기서는 raw_text 저장)
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

            # 멘션 외 모든 경로: 이미지 분석을 먼저 수행하여 RAG에 이미지 내용을 포함시킨다.
            image_ctx = analyze_slack_files(event.get("files") or [], config.SLACK_BOT_TOKEN)
            if image_ctx:
                if raw_text.strip():
                    effective_content = f"[첨부 이미지 분석]\n{image_ctx}\n\n{raw_text}".strip()
                else:
                    effective_content = f"[첨부 이미지 분석]\n{image_ctx}"
            else:
                effective_content = raw_text

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

            # 분류기 실행 (이미지 분석 결과 제외, 원문으로 분류)
            classify_result = classify_message(
                message=raw_text,
                is_mention=False,
                bot_user_id=bot_user_id,
                sender_user_id=user_id,
            )

            # 메시지 저장 (이미지 분석 포함 내용, 질문 여부 포함)
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
            )

            # 질문/요청인 경우만 답변 생성
            if not classify_result.is_actionable:
                return

            user_name = get_user_display_name(client, user_id) if user_id else "익명"
            thinking_ts = post_thinking_indicator(
                client=client, channel=channel_id, thread_ts=thread_ts or message_ts
            )

            # 스레드 문맥 조회 및 요약
            thread_summary = None
            if thread_ts:
                thread_msgs = fetch_thread_history(client, channel_id, thread_ts, limit=20)
                if thread_msgs and len(thread_msgs) >= 3:
                    thread_summary = summarize_thread_context(thread_msgs[:-1])

            _process_question(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts or message_ts,
                message_ts=message_ts,
                question=effective_content,
                user_id=user_id,
                user_name=user_name,
                session_factory=session_factory,
                thinking_ts=thinking_ts,
                thread_summary=thread_summary,
            )

        threading.Thread(target=worker, daemon=True).start()
