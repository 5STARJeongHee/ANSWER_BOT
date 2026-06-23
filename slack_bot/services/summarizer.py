# 채널 대화 요약 배치 서비스 - 주기적으로 대화를 압축하여 저장
from __future__ import annotations
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

import config
from db.repository import get_messages_in_period, get_latest_summary, save_summary
from services.llm_service import call_summary

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "너는 사내 Slack 채널의 대화를 요약하는 어시스턴트다. "
    "이후 챗봇이 참고할 수 있도록 다음 항목으로 한국어 3문장 이내로 요약한다.\n"
    "1. 주요 논의/이슈\n"
    "2. 결정된 사항\n"
    "3. 자주 반복되는 질문/주제\n"
    "대화 로그가 없으면 '요약할 대화 없음'이라고 반환한다."
)


def _build_conversation_log(messages: list) -> str:
    """메시지 목록을 요약용 텍스트로 변환한다."""
    if not messages:
        return ""

    lines = []
    for msg in messages:
        role_label = "봇" if msg.role == "bot" else f"사용자({msg.user_id or '?'})"
        content = (msg.content or "").strip()
        if content:
            lines.append(f"[{role_label}]: {content}")

    return "\n".join(lines)


def summarize_channel(
    session: Session,
    channel_id: str,
    period_start: date,
    period_end: date,
) -> Optional[str]:
    """
    지정 채널의 기간 대화를 요약하고 context_summary 테이블에 저장한다.
    요약 텍스트를 반환하고, 실패 시 None을 반환한다.
    """
    start_dt = datetime.combine(period_start, datetime.min.time())
    end_dt = datetime.combine(period_end, datetime.max.time())

    messages = get_messages_in_period(
        session=session,
        channel_id=channel_id,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    if not messages:
        logger.info(f"요약할 메시지 없음 (channel={channel_id}, 기간={period_start}~{period_end})")
        return None

    conversation_log = _build_conversation_log(messages)
    logger.info(
        f"요약 시작 (channel={channel_id}, 메시지 수={len(messages)}, "
        f"기간={period_start}~{period_end})"
    )

    llm_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"채널: {channel_id}\n"
                f"기간: {period_start} ~ {period_end}\n\n"
                f"[대화 시작]\n{conversation_log[:8000]}\n[대화 끝]"
            ),
        },
    ]

    summary_text = call_summary(llm_messages)
    if not summary_text:
        logger.error(f"요약 생성 실패 (channel={channel_id})")
        return None

    save_summary(
        session=session,
        channel_id=channel_id,
        period_start=period_start,
        period_end=period_end,
        summary_text=summary_text,
    )
    session.commit()
    logger.info(f"요약 저장 완료 (channel={channel_id})")
    return summary_text


def summarize_thread_context(thread_messages: list[dict]) -> Optional[str]:
    """
    주어진 스레드 메시지(딕셔너리 리스트)를 짧은 문맥으로 요약한다.
    스레드가 너무 길 경우 토큰 제한을 막기 위해 사용된다.
    """
    if not thread_messages:
        return None

    # Slack API dict 형식을 단순 텍스트 로그로 변환
    lines = []
    for msg in thread_messages:
        user_id = msg.get("user") or msg.get("bot_id") or "?"
        text = (msg.get("text") or "").strip()
        if text:
            lines.append(f"[{user_id}]: {text}")
    
    conversation_log = "\n".join(lines)
    if not conversation_log.strip():
        return None

    prompt = (
        "다음은 슬랙 스레드의 이전 대화 내용이다. "
        "마지막 질문에 답변하기 위해 필요한 핵심 문맥과 흐름을 2~3문장 이내로 짧게 요약하라.\n\n"
        f"[스레드 대화 시작]\n{conversation_log[:4000]}\n[대화 끝]"
    )

    llm_messages = [
        {"role": "system", "content": "너는 대화의 핵심 문맥을 짧게 파악하는 AI다."},
        {"role": "user", "content": prompt},
    ]

    summary = call_summary(llm_messages)
    return summary.strip() if summary else None


def run_summary_batch(session: Session, period_days: int = 7) -> None:
    """
    모든 대상 채널에 대해 최근 period_days일의 대화를 요약하는 배치를 실행한다.
    APScheduler에 의해 호출된다.
    """
    today = date.today()
    period_end = today - timedelta(days=1)
    period_start = period_end - timedelta(days=period_days - 1)

    logger.info(f"요약 배치 시작 ({period_start} ~ {period_end}, {period_days}일 기간)")

    for channel_id in config.TARGET_CHANNEL_IDS:
        try:
            result = summarize_channel(
                session=session,
                channel_id=channel_id,
                period_start=period_start,
                period_end=period_end,
            )
            if result:
                logger.info(f"채널 {channel_id} 요약 완료")
            else:
                logger.info(f"채널 {channel_id} 요약 스킵 (메시지 없음 또는 실패)")
        except Exception as exc:
            logger.error(f"채널 {channel_id} 요약 오류: {exc}", exc_info=True)

    logger.info("요약 배치 완료")


def run_weekly_summary(session: Session) -> None:
    """하위 호환 래퍼 — 주간(7일) 배치를 실행한다."""
    run_summary_batch(session=session, period_days=7)
