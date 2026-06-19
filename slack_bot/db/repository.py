# 데이터베이스 CRUD 함수 모음 - 모든 DB 접근은 이 모듈을 통한다
from __future__ import annotations
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import ConversationMessage, ContextEmbedding, ContextSummary, MessageFeedback
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
    force: bool = False,
) -> Optional[ConversationMessage]:
    """
    메시지를 저장한다.
    - force=False(기본): event_id 또는 (channel_id, message_ts) 기준 중복이면 None을 반환한다.
    - force=True: 기존 메시지가 있으면 content를 갱신하고 기존 임베딩을 삭제(재생성 유도)한다.
    """
    # 중복 체크: event_id 우선, 없으면 (channel_id, message_ts) 확인
    if event_id and not force:
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
        if not force:
            logger.debug(f"중복 메시지 스킵: channel={channel_id} ts={message_ts}")
            return None

        # force=True: content 갱신 + 기존 임베딩 삭제 (호출자가 재생성)
        existing.content = content
        if is_question is not None:
            existing.is_question = is_question
        session.query(ContextEmbedding).filter(
            ContextEmbedding.source_message_id == existing.id
        ).delete(synchronize_session=False)
        session.flush()
        logger.debug(f"강제 갱신: channel={channel_id} ts={message_ts} id={existing.id}")
        return existing

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
    chunk_type: str = "message",
) -> ContextEmbedding:
    """임베딩을 저장한다. pgvector 비활성 시 JSON 직렬화로 저장한다."""
    import json

    emb = ContextEmbedding(
        source_message_id=source_message_id,
        chunk_text=chunk_text,
        chunk_type=chunk_type,
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
    query_text: str = "",
    channel_id: Optional[str] = None,
    top_k: int = 5,
) -> list[dict]:
    """
    쿼리 임베딩과 가장 유사한 청크를 반환한다.
    ENABLE_HYBRID_SEARCH=true이면 pg_trgm + pgvector RRF 검색,
    false이면 순수 벡터 검색, pgvector 비활성 시 텍스트 fallback.
    """
    if config.ENABLE_VECTOR_SEARCH and query_embedding:
        if config.ENABLE_HYBRID_SEARCH and query_text:
            return _hybrid_search(session, query_embedding, query_text, channel_id, top_k)
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
        channel_filter = "WHERE m.channel_id = :channel_id"
        params["channel_id"] = channel_id

    # CAST(:vec AS vector) 사용 — :vec::vector 형태는 SQLAlchemy 파라미터 파싱과 충돌
    sql = text(
        f"SELECT ce.chunk_text, "
        f"1 - (ce.embedding <=> CAST(:vec AS vector)) AS similarity, "
        f"ce.source_message_id, m.role, ce.chunk_type "
        f"FROM context_embedding ce "
        f"JOIN conversation_message m ON ce.source_message_id = m.id "
        f"{channel_filter} "
        f"ORDER BY ce.embedding <=> CAST(:vec AS vector) "
        f"LIMIT :top_k"
    )
    try:
        rows = session.execute(sql, params).fetchall()
        return [
            {
                "chunk_text": r[0],
                "similarity": float(r[1]),
                "message_id": r[2],
                "role": r[3],
                "chunk_type": r[4] or "message",
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning(f"pgvector 검색 실패, fallback: {exc}")
        session.rollback()
        return _text_fallback_search(session, channel_id, top_k)


def _hybrid_search(
    session: Session,
    query_embedding: list[float],
    query_text: str,
    channel_id: Optional[str],
    top_k: int,
) -> list[dict]:
    """
    pg_trgm(trigram 키워드) + pgvector(코사인) 검색을 RRF로 통합한다.
    언어 무관 trigram은 에러코드·한글 모두 처리한다.
    pg_trgm 미설치 또는 오류 시 순수 벡터 검색으로 fallback한다.
    """
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    pool = max(top_k * 4, 20)  # 각 검색에서 가져올 후보 수
    rrf_k = 60  # RRF 표준 상수

    channel_where = ""
    params: dict = {"vec": vec_str, "query_text": query_text, "pool": pool, "top_k": top_k}
    if channel_id:
        channel_where = "AND m.channel_id = :channel_id"
        params["channel_id"] = channel_id

    sql = text(f"""
        WITH vector_ranked AS (
            SELECT ce.id,
                   ce.chunk_text,
                   ce.source_message_id,
                   m.role,
                   ce.chunk_type,
                   1 - (ce.embedding <=> CAST(:vec AS vector)) AS vec_sim,
                   ROW_NUMBER() OVER (ORDER BY ce.embedding <=> CAST(:vec AS vector)) AS vec_rank
            FROM context_embedding ce
            JOIN conversation_message m ON ce.source_message_id = m.id
            WHERE ce.embedding IS NOT NULL
            {channel_where}
            ORDER BY ce.embedding <=> CAST(:vec AS vector)
            LIMIT :pool
        ),
        trgm_ranked AS (
            SELECT ce.id,
                   ce.chunk_text,
                   ce.source_message_id,
                   m.role,
                   ce.chunk_type,
                   word_similarity(:query_text, ce.chunk_text) AS trgm_sim,
                   ROW_NUMBER() OVER (
                       ORDER BY word_similarity(:query_text, ce.chunk_text) DESC
                   ) AS trgm_rank
            FROM context_embedding ce
            JOIN conversation_message m ON ce.source_message_id = m.id
            WHERE word_similarity(:query_text, ce.chunk_text) > 0.1
            {channel_where}
            ORDER BY trgm_sim DESC
            LIMIT :pool
        ),
        merged AS (
            SELECT
                COALESCE(v.id, t.id) AS id,
                COALESCE(v.chunk_text, t.chunk_text) AS chunk_text,
                COALESCE(v.source_message_id, t.source_message_id) AS source_message_id,
                COALESCE(v.role, t.role) AS role,
                COALESCE(v.chunk_type, t.chunk_type) AS chunk_type,
                COALESCE(1.0 / ({rrf_k} + v.vec_rank), 0.0)
                    + COALESCE(1.0 / ({rrf_k} + t.trgm_rank), 0.0) AS rrf_score
            FROM vector_ranked v
            FULL OUTER JOIN trgm_ranked t ON v.id = t.id
        )
        SELECT chunk_text, rrf_score, source_message_id, role, chunk_type
        FROM merged
        ORDER BY rrf_score DESC
        LIMIT :top_k
    """)
    try:
        rows = session.execute(sql, params).fetchall()
        return [
            {
                "chunk_text": r[0],
                "similarity": float(r[1]),
                "message_id": r[2],
                "role": r[3],
                "chunk_type": r[4] or "message",
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning(f"Hybrid 검색 실패, 벡터 검색으로 fallback: {exc}")
        session.rollback()
        return _vector_search(session, query_embedding, channel_id, top_k)


def _text_fallback_search(
    session: Session,
    channel_id: Optional[str],
    top_k: int,
) -> list[dict]:
    """pgvector 없을 때 최근 메시지를 반환하는 fallback 검색."""
    query = session.query(ContextEmbedding, ConversationMessage.role).join(
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
        {
            "chunk_text": r[0].chunk_text,
            "similarity": 0.0,
            "message_id": r[0].source_message_id,
            "role": r[1],
            "chunk_type": r[0].chunk_type or "message",
        }
        for r in reversed(rows)
    ]


def save_thread_chunk_embedding(
    session: Session,
    *,
    channel_id: str,
    thread_ts: str,
    embed_fn,
) -> Optional[ContextEmbedding]:
    """
    스레드 내 모든 메시지를 Q:/A: 형식으로 합쳐 하나의 thread 청크로 임베딩한다.
    source_message_id는 스레드 첫 메시지 ID를 사용하며, 기존 thread 청크가 있으면 갱신한다.
    단일 메시지 스레드(2개 미만)는 처리하지 않는다.
    """
    import json

    msgs = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.thread_ts == thread_ts,
        )
        .order_by(ConversationMessage.created_at.asc())
        .all()
    )
    if not msgs or len(msgs) < 2:
        return None

    parts = []
    for msg in msgs:
        prefix = "Q:" if msg.role == "user" else "A:"
        parts.append(f"{prefix} {msg.content.strip()}")
    chunk_text = "\n".join(parts)

    if len(chunk_text) > config.THREAD_CHUNK_MAX_CHARS:
        chunk_text = chunk_text[:config.THREAD_CHUNK_MAX_CHARS]

    first_msg = msgs[0]

    # 기존 thread 청크가 있으면 내용만 갱신한다
    existing = (
        session.query(ContextEmbedding)
        .filter(
            ContextEmbedding.source_message_id == first_msg.id,
            ContextEmbedding.chunk_type == "thread",
        )
        .first()
    )

    embedding = embed_fn(chunk_text)

    if existing:
        existing.chunk_text = chunk_text
        if embedding:
            existing.embedding_json = json.dumps(embedding)
            if config.ENABLE_VECTOR_SEARCH:
                vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
                try:
                    session.execute(text("SAVEPOINT thread_chunk_vec"))
                    session.execute(
                        text(
                            "UPDATE context_embedding SET embedding = CAST(:vec AS vector) "
                            "WHERE id = :id"
                        ),
                        {"vec": vec_str, "id": existing.id},
                    )
                    session.execute(text("RELEASE SAVEPOINT thread_chunk_vec"))
                except Exception:
                    session.execute(text("ROLLBACK TO SAVEPOINT thread_chunk_vec"))
        session.flush()
        return existing

    return save_embedding(
        session=session,
        source_message_id=first_msg.id,
        chunk_text=chunk_text,
        embedding=embedding,
        chunk_type="thread",
    )


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


def save_feedback(
    session: Session,
    *,
    channel_id: str,
    message_ts: str,
    user_id: str,
    reaction: str,
    sentiment: str,
) -> Optional[MessageFeedback]:
    """
    이모지 피드백을 저장한다.
    동일 (message_ts, user_id, reaction) 조합이 이미 존재하면 None을 반환한다.
    """
    fb = MessageFeedback(
        channel_id=channel_id,
        message_ts=message_ts,
        user_id=user_id,
        reaction=reaction,
        sentiment=sentiment,
    )
    try:
        session.add(fb)
        session.flush()
        return fb
    except IntegrityError:
        session.rollback()
        logger.debug(f"중복 피드백 무시: ts={message_ts} user={user_id} reaction={reaction}")
        return None


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



def get_last_message_ts(
    session: Session,
    channel_id: str,
) -> Optional[str]:
    """
    채널에서 가장 최근 message_ts를 반환한다.
    백필 시 이미 수집된 구간을 건너뛰기 위한 기준점으로 사용한다.
    메시지가 없으면 None을 반환한다.
    """
    msg = (
        session.query(ConversationMessage.message_ts)
        .filter(ConversationMessage.channel_id == channel_id)
        .order_by(ConversationMessage.message_ts.desc())
        .first()
    )
    return msg[0] if msg else None


def get_oldest_message_ts(
    session: Session,
    channel_id: str,
) -> Optional[str]:
    """
    채널에서 가장 오래된 message_ts를 반환한다.
    백필 재실행 시 아직 수집되지 않은 과거 구간의 상한선으로 사용한다.
    메시지가 없으면 None을 반환한다.
    """
    msg = (
        session.query(ConversationMessage.message_ts)
        .filter(ConversationMessage.channel_id == channel_id)
        .order_by(ConversationMessage.message_ts.asc())
        .first()
    )
    return msg[0] if msg else None


def get_thread_starter_user_id(
    session: Session,
    channel_id: str,
    thread_ts: str,
) -> Optional[str]:
    """스레드 원글 작성자 user_id를 반환한다. 원글이 DB에 없으면 None."""
    msg = session.query(ConversationMessage).filter(
        ConversationMessage.channel_id == channel_id,
        ConversationMessage.message_ts == thread_ts,
    ).first()
    return msg.user_id if msg else None

def has_negative_feedback(session: Session, message_id: int) -> bool:
    """
    RAG 결과의 message_id에 해당하는 메시지에 순부정 피드백이 있는지 확인한다.
    conversation_message.message_ts -> message_feedback.message_ts 경유 조회.
    """
    msg = session.query(ConversationMessage).filter(
        ConversationMessage.id == message_id
    ).first()
    if not msg:
        return False

    feedbacks = session.query(MessageFeedback).filter(
        MessageFeedback.message_ts == msg.message_ts
    ).all()
    if not feedbacks:
        return False

    neg = sum(1 for f in feedbacks if f.sentiment == "negative")
    pos = sum(1 for f in feedbacks if f.sentiment == "positive")
    return neg > pos
