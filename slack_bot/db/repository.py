# 데이터베이스 CRUD 함수 모음 - 모든 DB 접근은 이 모듈을 통한다
from __future__ import annotations
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import ConversationMessage, ContextEmbedding, ContextSummary
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConversationMessage CRUD
# ---------------------------------------------------------------------------

def upsert_message(
    session: Session,
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
) -> Optional[ConversationMessage]:
    """
    메시지를 저장한다. event_id 또는 (channel_id, message_ts) 기준 중복이면 None을 반환한다.
    """
    # 중복 체크: event_id 우선, 없으면 (channel_id, message_ts) 확인
    if event_id:
        existing = (
            session.query(ConversationMessage)
            .filter(ConversationMessage.event_id == event_id)
            .first()
        )
        if existing:
            logger.debug(f"중복 이벤트 스킵: event_id={event_id}")
            return None

    existing = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.message_ts == message_ts,
        )
        .first()
    )
    if existing:
        logger.debug(f"중복 메시지 스킵: channel={channel_id} ts={message_ts}")
        return None

    msg = ConversationMessage(
        event_id=event_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        message_ts=message_ts,
        user_id=user_id,
        role=role,
        content=content,
        is_question=is_question,
        is_fallback=is_fallback,
    )
    try:
        session.add(msg)
        session.flush()  # id 획득용 (commit은 호출자 책임)
        return msg
    except IntegrityError:
        session.rollback()
        logger.debug(f"IntegrityError - 중복 메시지 무시: channel={channel_id} ts={message_ts}")
        return None


def get_recent_messages(
    session: Session,
    channel_id: str,
    thread_ts: Optional[str] = None,
    limit: int = 5,
) -> list[ConversationMessage]:
    """최근 N개 메시지를 오래된 순으로 반환한다."""
    query = session.query(ConversationMessage).filter(
        ConversationMessage.channel_id == channel_id
    )
    if thread_ts:
        query = query.filter(ConversationMessage.thread_ts == thread_ts)

    messages = (
        query.order_by(ConversationMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(messages))


def get_messages_in_period(
    session: Session,
    channel_id: str,
    start_dt: datetime,
    end_dt: datetime,
    limit: int = 500,
) -> list[ConversationMessage]:
    """지정 기간의 메시지 목록을 반환한다 (요약 배치용)."""
    return (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.created_at >= start_dt,
            ConversationMessage.created_at <= end_dt,
        )
        .order_by(ConversationMessage.created_at.asc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# ContextEmbedding CRUD
# ---------------------------------------------------------------------------

def save_embedding(
    session: Session,
    *,
    source_message_id: int,
    chunk_text: str,
    embedding: Optional[list[float]] = None,
) -> ContextEmbedding:
    """임베딩을 저장한다. pgvector 비활성 시 JSON 직렬화로 저장한다."""
    import json

    emb = ContextEmbedding(
        source_message_id=source_message_id,
        chunk_text=chunk_text,
        embedding_json=json.dumps(embedding) if embedding else None,
    )
    session.add(emb)
    session.flush()

    # pgvector 활성화 시 vector 컬럼에도 저장
    # SAVEPOINT를 사용해 실패 시 기존 INSERT가 롤백되지 않도록 보호한다.
    # CAST(:vec AS vector) 사용 — :vec::vector 형태는 SQLAlchemy 파라미터 파싱과 충돌
    if config.ENABLE_VECTOR_SEARCH and embedding:
        try:
            vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            session.execute(text("SAVEPOINT pgvec_update"))
            session.execute(
                text(
                    "UPDATE context_embedding SET embedding = CAST(:vec AS vector) "
                    "WHERE id = :id"
                ),
                {"vec": vec_str, "id": emb.id},
            )
            session.execute(text("RELEASE SAVEPOINT pgvec_update"))
        except Exception as exc:
            session.execute(text("ROLLBACK TO SAVEPOINT pgvec_update"))
            logger.warning(f"pgvector 저장 실패 (fallback JSON 유지): {exc}")

    return emb


def search_similar_embeddings(
    session: Session,
    query_embedding: list[float],
    channel_id: Optional[str] = None,
    top_k: int = 5,
) -> list[dict]:
    """
    쿼리 임베딩과 가장 유사한 청크를 반환한다.
    pgvector 비활성 시 최근 메시지 full-text 검색으로 fallback한다.
    """
    if config.ENABLE_VECTOR_SEARCH and query_embedding:
        return _vector_search(session, query_embedding, channel_id, top_k)
    else:
        return _text_fallback_search(session, channel_id, top_k)


def _vector_search(
    session: Session,
    query_embedding: list[float],
    channel_id: Optional[str],
    top_k: int,
) -> list[dict]:
    """pgvector 코사인 유사도 검색."""
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    channel_filter = ""
    params: dict = {"vec": vec_str, "top_k": top_k}
    if channel_id:
        channel_filter = (
            "JOIN conversation_message m ON ce.source_message_id = m.id "
            "WHERE m.channel_id = :channel_id"
        )
        params["channel_id"] = channel_id

    # CAST(:vec AS vector) 사용 — :vec::vector 형태는 SQLAlchemy 파라미터 파싱과 충돌
    sql = text(
        f"SELECT ce.chunk_text, "
        f"1 - (ce.embedding <=> CAST(:vec AS vector)) AS similarity, "
        f"ce.source_message_id "
        f"FROM context_embedding ce "
        f"{channel_filter} "
        f"ORDER BY ce.embedding <=> CAST(:vec AS vector) "
        f"LIMIT :top_k"
    )
    try:
        rows = session.execute(sql, params).fetchall()
        return [
            {"chunk_text": r[0], "similarity": float(r[1]), "message_id": r[2]}
            for r in rows
        ]
    except Exception as exc:
        logger.warning(f"pgvector 검색 실패, fallback: {exc}")
        session.rollback()
        return _text_fallback_search(session, channel_id, top_k)


def _text_fallback_search(
    session: Session,
    channel_id: Optional[str],
    top_k: int,
) -> list[dict]:
    """pgvector 없을 때 최근 메시지를 반환하는 fallback 검색."""
    query = session.query(ContextEmbedding).join(
        ConversationMessage,
        ContextEmbedding.source_message_id == ConversationMessage.id,
    )
    if channel_id:
        query = query.filter(ConversationMessage.channel_id == channel_id)

    rows = (
        query.order_by(ContextEmbedding.id.desc())
        .limit(top_k)
        .all()
    )
    return [
        {"chunk_text": r.chunk_text, "similarity": 0.0, "message_id": r.source_message_id}
        for r in reversed(rows)
    ]


# ---------------------------------------------------------------------------
# ContextSummary CRUD
# ---------------------------------------------------------------------------

def save_summary(
    session: Session,
    *,
    channel_id: str,
    period_start: date,
    period_end: date,
    summary_text: str,
) -> ContextSummary:
    """채널 대화 요약본을 저장한다."""
    summary = ContextSummary(
        channel_id=channel_id,
        period_start=period_start,
        period_end=period_end,
        summary_text=summary_text,
    )
    session.add(summary)
    session.flush()
    return summary


def get_latest_summary(
    session: Session,
    channel_id: str,
) -> Optional[ContextSummary]:
    """채널의 가장 최근 요약본을 반환한다."""
    return (
        session.query(ContextSummary)
        .filter(ContextSummary.channel_id == channel_id)
        .order_by(ContextSummary.period_end.desc())
        .first()
    )
