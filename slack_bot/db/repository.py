# 데이터베이스 CRUD 함수 모음 - 모든 DB 접근은 이 모듈을 통한다
from __future__ import annotations
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import BotSetting, ConversationMessage, ContextEmbedding, ContextSummary, MessageAttachment, MessageFeedback, ProductCategory
from utils.attachment_result import AttachmentResult
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
    response_time_ms: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    rag_avg_similarity: Optional[float] = None,
    used_web_search: bool = False,
    topic: Optional[str] = None,
    product_key: Optional[str] = None,
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
        if response_time_ms is not None:
            existing.response_time_ms = response_time_ms
        if prompt_tokens is not None:
            existing.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            existing.completion_tokens = completion_tokens
        if rag_avg_similarity is not None:
            existing.rag_avg_similarity = rag_avg_similarity
        existing.used_web_search = used_web_search
        if topic is not None:
            existing.topic = topic
        if product_key is not None:
            existing.product_key = product_key
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
        response_time_ms=response_time_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        rag_avg_similarity=rag_avg_similarity,
        used_web_search=used_web_search,
        topic=topic,
        product_key=product_key,
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
    topic: Optional[str] = None,
) -> list[dict]:
    """
    쿼리 임베딩과 가장 유사한 청크를 반환한다.
    ENABLE_HYBRID_SEARCH=true이면 pg_trgm + pgvector RRF 검색,
    false이면 순수 벡터 검색, pgvector 비활성 시 텍스트 fallback.
    topic이 제공될 경우 해당 주제를 가진 과거 문맥에 유사도 보너스(0.1)를 부여한다.
    """
    if config.ENABLE_VECTOR_SEARCH and query_embedding:
        if config.ENABLE_HYBRID_SEARCH and query_text:
            return _hybrid_search(session, query_embedding, query_text, channel_id, top_k, topic)
        return _vector_search(session, query_embedding, channel_id, top_k, topic)
    else:
        return _text_fallback_search(session, channel_id, top_k)


def _vector_search(
    session: Session,
    query_embedding: list[float],
    channel_id: Optional[str],
    top_k: int,
    topic: Optional[str] = None,
) -> list[dict]:
    """pgvector 코사인 유사도 검색."""
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    channel_filter = ""
    params: dict = {"vec": vec_str, "top_k": top_k}
    if channel_id:
        channel_filter = "WHERE m.channel_id = :channel_id"
        params["channel_id"] = channel_id

    topic_boost_sql = ""
    if topic and topic != "미분류":
        topic_boost_sql = " + CASE WHEN m.topic = :topic THEN 0.1 ELSE 0.0 END"
        params["topic"] = topic

    # 서브쿼리로 similarity를 한 번만 계산하고 외부 ORDER BY에서 컬럼명으로 참조한다.
    # CAST(:vec AS vector) 사용 — :vec::vector 형태는 SQLAlchemy 파라미터 파싱과 충돌
    sql = text(
        f"SELECT chunk_text, similarity, source_message_id, role, chunk_type "
        f"FROM ("
        f"  SELECT ce.chunk_text,"
        f"  1 - (ce.embedding <=> CAST(:vec AS vector)){topic_boost_sql} AS similarity,"
        f"  ce.source_message_id, m.role, ce.chunk_type "
        f"  FROM context_embedding ce "
        f"  JOIN conversation_message m ON ce.source_message_id = m.id "
        f"  {channel_filter}"
        f") sub "
        f"ORDER BY similarity DESC "
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
    topic: Optional[str] = None,
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
        
    topic_boost_sql = ""
    if topic and topic != "미분류":
        topic_boost_sql = " + CASE WHEN COALESCE(v.topic, t.topic) = :topic THEN 0.1 ELSE 0.0 END "
        params["topic"] = topic

    sql = text(f"""
        WITH vector_ranked AS (
            SELECT ce.id,
                   ce.chunk_text,
                   ce.source_message_id,
                   m.role,
                   ce.chunk_type,
                   m.topic,
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
                   m.topic,
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
                    + COALESCE(1.0 / ({rrf_k} + t.trgm_rank), 0.0)
                    {topic_boost_sql} AS rrf_score
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


def save_mention_chain_embedding(
    session: Session,
    *,
    channel_id: str,
    message_ts_list: list[str],
    embed_fn,
) -> Optional[ContextEmbedding]:
    """
    비스레드 @mention 대화 체인을 Q:/A: 형식으로 합쳐 conversation 청크로 임베딩한다.
    message_ts_list: 체인에 속한 Slack message_ts 값 목록 (시간순).
    2개 미만 메시지는 처리하지 않는다.
    """
    import json

    msgs = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.message_ts.in_(message_ts_list),
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

    existing = (
        session.query(ContextEmbedding)
        .filter(
            ContextEmbedding.source_message_id == first_msg.id,
            ContextEmbedding.chunk_type == "conversation",
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
                    session.execute(text("SAVEPOINT conv_chunk_vec"))
                    session.execute(
                        text(
                            "UPDATE context_embedding SET embedding = CAST(:vec AS vector) "
                            "WHERE id = :id"
                        ),
                        {"vec": vec_str, "id": existing.id},
                    )
                    session.execute(text("RELEASE SAVEPOINT conv_chunk_vec"))
                except Exception:
                    session.execute(text("ROLLBACK TO SAVEPOINT conv_chunk_vec"))
        session.flush()
        return existing

    return save_embedding(
        session=session,
        source_message_id=first_msg.id,
        chunk_text=chunk_text,
        embedding=embedding,
        chunk_type="conversation",
    )


def save_session_window_embedding(
    session: Session,
    *,
    channel_id: str,
    current_message_ts: str,
    embed_fn,
    window_minutes: int = 5,
) -> Optional[ContextEmbedding]:
    """
    비스레드 채널에서 current_message_ts 기준 window_minutes 내 메시지를
    멀티스피커 포맷으로 합쳐 session 청크로 임베딩한다.
    앵커는 윈도우 내 첫 메시지 ID를 사용하며, 기존 session 청크가 있으면 갱신한다.
    2개 미만 메시지는 처리하지 않는다.
    """
    import json

    try:
        current_ts_float = float(current_message_ts)
    except (ValueError, TypeError):
        return None

    window_start_float = current_ts_float - window_minutes * 60

    # 최근 비스레드 사용자 메시지 100개를 가져온 뒤 Python에서 시간 필터링한다.
    # message_ts가 Slack epoch float 문자열이므로 SQL CAST 없이 Python에서 파싱하는 게 안전하다.
    candidates = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.thread_ts == None,  # noqa: E711
            ConversationMessage.role == "user",
        )
        .order_by(ConversationMessage.message_ts.desc())
        .limit(100)
        .all()
    )

    msgs = []
    for msg in candidates:
        try:
            ts = float(msg.message_ts)
        except (ValueError, TypeError):
            continue
        if window_start_float <= ts <= current_ts_float:
            msgs.append((ts, msg))

    msgs.sort(key=lambda x: x[0])
    msgs = [m for _, m in msgs]

    if len(msgs) < 2:
        return None

    # 발화자가 바뀔 때만 [user_id] 레이블을 붙여 멀티스피커 흐름을 표현한다
    parts = []
    prev_user = None
    for msg in msgs:
        if msg.user_id != prev_user:
            parts.append(f"[{msg.user_id or '사용자'}] {msg.content.strip()}")
            prev_user = msg.user_id
        else:
            parts.append(msg.content.strip())
    chunk_text = "\n".join(parts)

    if len(chunk_text) > config.THREAD_CHUNK_MAX_CHARS:
        chunk_text = chunk_text[:config.THREAD_CHUNK_MAX_CHARS]

    first_msg = msgs[0]

    existing = (
        session.query(ContextEmbedding)
        .filter(
            ContextEmbedding.source_message_id == first_msg.id,
            ContextEmbedding.chunk_type == "session",
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
                    session.execute(text("SAVEPOINT session_chunk_vec"))
                    session.execute(
                        text(
                            "UPDATE context_embedding SET embedding = CAST(:vec AS vector) "
                            "WHERE id = :id"
                        ),
                        {"vec": vec_str, "id": existing.id},
                    )
                    session.execute(text("RELEASE SAVEPOINT session_chunk_vec"))
                except Exception:
                    session.execute(text("ROLLBACK TO SAVEPOINT session_chunk_vec"))
        session.flush()
        return existing

    return save_embedding(
        session=session,
        source_message_id=first_msg.id,
        chunk_text=chunk_text,
        embedding=embedding,
        chunk_type="session",
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


def get_bot_message_with_question(
    session: Session,
    channel_id: str,
    message_ts: str,
) -> tuple[Optional[ConversationMessage], Optional[ConversationMessage]]:
    """
    봇 답변 ts로 봇 메시지와 같은 스레드의 직전 사용자 질문을 반환한다.
    (bot_msg, question_msg) 형태로 반환하며, 없으면 해당 요소가 None이다.
    """
    bot_msg = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.message_ts == message_ts,
            ConversationMessage.role == "bot",
        )
        .first()
    )
    if not bot_msg:
        return None, None

    thread_filter = (
        ConversationMessage.thread_ts == bot_msg.thread_ts
        if bot_msg.thread_ts
        else ConversationMessage.channel_id == channel_id
    )
    question_msg = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            thread_filter,
            ConversationMessage.role == "user",
            ConversationMessage.is_question.is_(True),
        )
        .order_by(ConversationMessage.created_at.desc())
        .first()
    )
    return bot_msg, question_msg


def update_feedback_failure_reason(
    session: Session,
    *,
    message_ts: str,
    user_id: str,
    llm_reason: Optional[str] = None,
    user_reason: Optional[str] = None,
) -> bool:
    """
    기존 피드백 행의 실패 원인을 갱신한다. 갱신 성공 시 True를 반환한다.
    llm_reason, user_reason 중 전달된 것만 덮어쓴다.
    """
    fb = (
        session.query(MessageFeedback)
        .filter(
            MessageFeedback.message_ts == message_ts,
            MessageFeedback.user_id == user_id,
            MessageFeedback.sentiment == "negative",
        )
        .first()
    )
    if not fb:
        return False
    if llm_reason is not None:
        fb.llm_failure_reason = llm_reason
    if user_reason is not None:
        fb.user_failure_reason = user_reason
    session.flush()
    return True


def save_qa_feedback_embedding(
    session_factory,
    *,
    bot_msg_id: int,
    question_text: str,
    answer_text: str,
) -> bool:
    """
    긍정 피드백을 받은 봇 답변을 Q:/A: QA 쌍으로 임베딩하여 저장한다.
    이미 qa_feedback 청크가 존재하면 중복 저장하지 않는다.
    """
    import json

    from services.context_retriever import embed_text

    session = session_factory()
    try:
        already = (
            session.query(ContextEmbedding)
            .filter(
                ContextEmbedding.source_message_id == bot_msg_id,
                ContextEmbedding.chunk_type == "qa_feedback",
            )
            .first()
        )
        if already:
            return False

        chunk_text = f"Q: {question_text.strip()}\nA: {answer_text.strip()}"
        embedding = embed_text(chunk_text)

        emb = ContextEmbedding(
            source_message_id=bot_msg_id,
            chunk_text=chunk_text,
            chunk_type="qa_feedback",
            embedding_json=json.dumps(embedding) if embedding else None,
        )
        session.add(emb)
        session.flush()

        if config.ENABLE_VECTOR_SEARCH and embedding:
            vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            try:
                session.execute(text("SAVEPOINT qa_feedback_vec"))
                session.execute(
                    text("UPDATE context_embedding SET embedding = CAST(:vec AS vector) WHERE id = :id"),
                    {"vec": vec_str, "id": emb.id},
                )
                session.execute(text("RELEASE SAVEPOINT qa_feedback_vec"))
            except Exception as exc:
                session.execute(text("ROLLBACK TO SAVEPOINT qa_feedback_vec"))
                logger.warning(f"QA 피드백 pgvector 저장 실패 (fallback JSON 유지): {exc}")

        session.commit()
        return True
    except Exception as exc:
        session.rollback()
        logger.error(f"QA 피드백 임베딩 저장 실패: {exc}", exc_info=True)
        return False
    finally:
        session.close()


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

def get_channel_question_history(
    session: Session,
    channel_id: str,
    limit: int = 20,
) -> list[ConversationMessage]:
    """채널의 질문·요청 이력을 최신순으로 반환한다 (전체 공유용)."""
    return (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.role == "user",
            ConversationMessage.is_question == True,
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit)
        .all()
    )


def get_channel_history_by_date(
    session: Session,
    channel_id: str,
    days: int = 7,
) -> dict[date, list[dict]]:
    """최근 N일 채널 메시지를 날짜 → 대화 그룹별로 묶어 Q/A 요약과 함께 반환한다."""
    from collections import defaultdict, OrderedDict
    from utils.conversation_grouper import group_messages_by_conversation

    since = datetime.utcnow() - timedelta(days=days)
    messages = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.created_at >= since,
        )
        .order_by(ConversationMessage.created_at.asc())
        .all()
    )

    msg_dicts = [
        {
            "user_id": (msg.user_id or "").upper(),
            "role": msg.role,
            "content": msg.content or "",
            "thread_ts": msg.thread_ts,
            "created_at": msg.created_at,
            "topic": msg.topic,
        }
        for msg in messages
    ]

    conv_groups = group_messages_by_conversation(msg_dicts)

    by_date: dict[date, list[dict]] = defaultdict(list)
    for group in conv_groups:
        if not group:
            continue
        day = group[0]["created_at"].date()
        topic = next((m["topic"] for m in group if m.get("topic")), None)
        first_q = next((m for m in group if m["role"] == "user"), None)
        first_a = next((m for m in group if m["role"] == "bot"), None)

        def _preview(m: dict | None) -> str:
            if not m:
                return ""
            text = m["content"][:50].replace("\n", " ")
            return text + "…" if len(m["content"]) > 50 else text

        user_ids = list(dict.fromkeys(
            m["user_id"] for m in group if m.get("user_id") and m["role"] == "user"
        ))
        by_date[day].append(
            {
                "topic": topic,
                "message_count": len(group),
                "q_preview": _preview(first_q),
                "a_preview": _preview(first_a),
                "user_ids": user_ids,
            }
        )

    result: dict[date, list[dict]] = OrderedDict()
    for day in sorted(by_date.keys(), reverse=True):
        result[day] = by_date[day]
    return result


def _group_messages_by_topic(
    messages: list,
    max_per_topic: int = 3,
    max_topics: int = 10,
) -> tuple[list[dict], int]:
    """
    메시지 목록(created_at asc 정렬 가정)을 스레드 단위로 묶고 topic별로 그룹핑한다.
    순수 인메모리 로직 — DB 의존성 없음.
    Returns: (topic_groups, total_question_count)
    """
    from collections import defaultdict as _dd

    # 스레드 단위 그룹핑 (effective key = thread_ts or message_ts)
    thread_msgs: dict[str, list] = _dd(list)
    for msg in messages:
        key = msg.thread_ts or msg.message_ts
        thread_msgs[key].append(msg)

    topic_counts: dict[str, int] = _dd(int)
    topic_entries: dict[str, list] = _dd(list)

    for msgs in thread_msgs.values():
        for i, msg in enumerate(msgs):
            if msg.role != "user" or not msg.is_question:
                continue
            topic = msg.topic or "미분류"
            topic_counts[topic] += 1

            if len(topic_entries[topic]) < max_per_topic:
                # 같은 스레드 내 이 질문 이후 첫 번째 봇 답변 찾기
                a_preview: Optional[str] = None
                for subsequent in msgs[i + 1:]:
                    if subsequent.role == "bot":
                        a_text = subsequent.content or ""
                        a_preview = a_text[:60] + ("…" if len(a_text) > 60 else "")
                        break

                q_text = msg.content or ""
                topic_entries[topic].append({
                    "q_preview": q_text[:60] + ("…" if len(q_text) > 60 else ""),
                    "a_preview": a_preview,
                    "created_at": msg.created_at,
                    "user_id": msg.user_id,
                })

    total_count = sum(topic_counts.values())

    sorted_topics = sorted(
        topic_counts.items(),
        key=lambda x: (x[0] == "미분류", -x[1]),
    )

    result = [
        {"topic": topic, "count": count, "entries": topic_entries[topic]}
        for topic, count in sorted_topics[:max_topics]
    ]
    return result, total_count


def get_channel_history_by_topic(
    session: Session,
    channel_id: str,
    days: int = 7,
    max_per_topic: int = 3,
    max_topics: int = 10,
) -> tuple[list[dict], int]:
    """
    최근 N일 채널 질문 이력을 canonical topic별로 묶어 반환한다.
    Returns: (topic_groups, total_question_count)
      topic_groups: [{topic, count, entries: [{q_preview, a_preview, created_at, user_id}]}, ...]
                     count 내림차순, 미분류 마지막.
    """
    since = datetime.utcnow() - timedelta(days=days)
    messages = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.created_at >= since,
        )
        .order_by(ConversationMessage.created_at.asc())
        .all()
    )
    return _group_messages_by_topic(messages, max_per_topic, max_topics)


def get_recent_qa_by_topic(
    session: Session,
    channel_id: str,
    topic: str,
    limit: int = 3,
) -> list[dict]:
    """
    동일한 주제(topic)의 최근 질문과 봇 답변을 반환한다.
    결과: [{"q_preview": str, "a_preview": str, "created_at": datetime}]
    """
    if not topic or topic == "미분류":
        return []

    # 해당 주제의 최근 사용자 질문 조회
    questions = (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.channel_id == channel_id,
            ConversationMessage.topic == topic,
            ConversationMessage.role == "user",
            ConversationMessage.is_question == True,  # noqa: E712
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit * 3)  # 답변이 없는 경우를 대비해 여유 있게 조회
        .all()
    )

    results = []
    seen_threads = set()
    for q in questions:
        thread_id = q.thread_ts or q.message_ts
        if thread_id in seen_threads:
            continue

        # 같은 스레드에서 질문 이후의 첫 번째 봇 답변 조회
        bot_msg = (
            session.query(ConversationMessage)
            .filter(
                ConversationMessage.channel_id == channel_id,
                ConversationMessage.thread_ts == thread_id,
                ConversationMessage.role == "bot",
                ConversationMessage.created_at >= q.created_at,
            )
            .order_by(ConversationMessage.created_at.asc())
            .first()
        )
        
        if bot_msg and bot_msg.content:
            q_text = q.content or ""
            a_text = bot_msg.content or ""
            results.append({
                "q_preview": q_text[:80] + ("…" if len(q_text) > 80 else ""),
                "a_preview": a_text[:120] + ("…" if len(a_text) > 120 else ""),
                "created_at": q.created_at,
            })
            seen_threads.add(thread_id)
            if len(results) >= limit:
                break

    return results


def get_dashboard_stats(session: Session, period_days: int = 7) -> dict:
    """챗봇 대시보드 집계 통계를 반환한다."""
    since = datetime.utcnow() - timedelta(days=period_days)

    total_responses = (
        session.query(func.count(ConversationMessage.id))
        .filter(
            ConversationMessage.role == "bot",
            ConversationMessage.is_fallback == False,
            ConversationMessage.created_at >= since,
        )
        .scalar()
    ) or 0

    fallback_count = (
        session.query(func.count(ConversationMessage.id))
        .filter(
            ConversationMessage.role == "bot",
            ConversationMessage.is_fallback == True,
            ConversationMessage.created_at >= since,
        )
        .scalar()
    ) or 0

    avg_response_ms = (
        session.query(func.avg(ConversationMessage.response_time_ms))
        .filter(
            ConversationMessage.role == "bot",
            ConversationMessage.response_time_ms.isnot(None),
            ConversationMessage.created_at >= since,
        )
        .scalar()
    )

    total_prompt_tokens = (
        session.query(func.sum(ConversationMessage.prompt_tokens))
        .filter(
            ConversationMessage.created_at >= since,
            ConversationMessage.prompt_tokens.isnot(None),
        )
        .scalar()
    ) or 0

    total_completion_tokens = (
        session.query(func.sum(ConversationMessage.completion_tokens))
        .filter(
            ConversationMessage.created_at >= since,
            ConversationMessage.completion_tokens.isnot(None),
        )
        .scalar()
    ) or 0

    feedback_rows = session.execute(
        text(
            "SELECT sentiment, COUNT(*) FROM message_feedback "
            "WHERE created_at >= :since GROUP BY sentiment"
        ),
        {"since": since},
    ).fetchall()
    feedback = {row[0]: int(row[1]) for row in feedback_rows}

    actionable_count = (
        session.query(func.count(ConversationMessage.id))
        .filter(
            ConversationMessage.role == "user",
            ConversationMessage.is_question == True,  # noqa: E712
            ConversationMessage.created_at >= since,
        )
        .scalar()
    ) or 0

    non_actionable_count = (
        session.query(func.count(ConversationMessage.id))
        .filter(
            ConversationMessage.role == "user",
            ConversationMessage.is_question == False,  # noqa: E712
            ConversationMessage.created_at >= since,
        )
        .scalar()
    ) or 0

    avg_rag_similarity = (
        session.query(func.avg(ConversationMessage.rag_avg_similarity))
        .filter(
            ConversationMessage.role == "bot",
            ConversationMessage.rag_avg_similarity.isnot(None),
            ConversationMessage.created_at >= since,
        )
        .scalar()
    )

    web_search_count = (
        session.query(func.count(ConversationMessage.id))
        .filter(
            ConversationMessage.role == "bot",
            ConversationMessage.used_web_search == True,
            ConversationMessage.created_at >= since,
        )
        .scalar()
    ) or 0

    active_users = (
        session.query(func.count(func.distinct(ConversationMessage.user_id)))
        .filter(
            ConversationMessage.role == "user",
            ConversationMessage.created_at >= since,
        )
        .scalar()
    ) or 0

    # 피드백 응답률: 피드백이 달린 봇 메시지 수 / 전체 봇 응답 수
    responded_with_feedback = session.execute(
        text(
            "SELECT COUNT(DISTINCT m.id) FROM conversation_message m "
            "JOIN message_feedback f ON f.message_ts = m.message_ts "
            "WHERE m.role='bot' AND m.created_at >= :since"
        ),
        {"since": since},
    ).scalar() or 0

    total_bot = int(total_responses) + int(fallback_count)

    return {
        "period_days": period_days,
        "total_responses": int(total_responses),
        "fallback_count": int(fallback_count),
        "avg_response_ms": int(avg_response_ms) if avg_response_ms else None,
        "total_prompt_tokens": int(total_prompt_tokens),
        "total_completion_tokens": int(total_completion_tokens),
        "positive_feedback": feedback.get("positive", 0),
        "negative_feedback": feedback.get("negative", 0),
        "actionable_count": int(actionable_count),
        "non_actionable_count": int(non_actionable_count),
        "avg_rag_similarity": round(float(avg_rag_similarity), 3) if avg_rag_similarity else None,
        "web_search_count": int(web_search_count),
        "web_search_rate": round(web_search_count / total_bot, 3) if total_bot else 0.0,
        "active_users": int(active_users),
        "feedback_response_rate": round(responded_with_feedback / total_bot, 3) if total_bot else 0.0,
    }


def get_recent_fallbacks(
    session: Session,
    period_days: int = 7,
    limit: int = 5,
) -> list[str]:
    """최근 fallback 트리거 질문 텍스트를 반환한다 (중복 제거, 최신순)."""
    since = datetime.utcnow() - timedelta(days=period_days)
    rows = (
        session.query(ConversationMessage.content)
        .filter(
            ConversationMessage.role == "bot",
            ConversationMessage.is_fallback == True,
            ConversationMessage.created_at >= since,
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit * 3)  # 중복 제거 여유분
        .all()
    )
    seen: set[str] = set()
    result: list[str] = []
    for (content,) in rows:
        text_val = content.removeprefix("[FALLBACK] ").strip()
        if text_val not in seen:
            seen.add(text_val)
            result.append(text_val)
        if len(result) >= limit:
            break
    return result


def get_top_topics(
    session: Session,
    period_days: int = 7,
    limit: int = 5,
) -> list[tuple[str, int]]:
    """최근 기간 동안 가장 많이 등장한 주제 태그를 빈도순으로 반환한다."""
    since = datetime.utcnow() - timedelta(days=period_days)
    rows = session.execute(
        text(
            "SELECT topic, COUNT(*) AS cnt "
            "FROM conversation_message "
            "WHERE role = 'user' AND is_question = TRUE "
            "  AND topic IS NOT NULL AND topic != '' "
            "  AND created_at >= :since "
            "GROUP BY topic "
            "ORDER BY cnt DESC "
            "LIMIT :limit"
        ),
        {"since": since, "limit": limit},
    ).fetchall()
    return [(row[0], int(row[1])) for row in rows]


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


# ---------------------------------------------------------------------------
# BotSetting CRUD
# ---------------------------------------------------------------------------

def get_bot_setting(session: Session, key: str) -> Optional[str]:
    """설정값을 반환한다. 없으면 None을 반환한다."""
    row = session.query(BotSetting).filter(BotSetting.key == key).first()
    return row.value if row else None


def save_bot_setting(session: Session, key: str, value: str) -> None:
    """설정값을 저장하거나 갱신한다."""
    row = session.query(BotSetting).filter(BotSetting.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        session.add(BotSetting(key=key, value=value, updated_at=datetime.utcnow()))
    session.flush()


_NOTIFICATION_ADMINS_KEY = "notification_admins"


def get_notification_admins(session: Session) -> list[str]:
    """알림 관리자 목록을 반환한다."""
    import json as _json
    raw = get_bot_setting(session, _NOTIFICATION_ADMINS_KEY)
    if not raw:
        return []
    try:
        return _json.loads(raw)
    except Exception:
        return []


def add_notification_admin(session: Session, user_id: str) -> bool:
    """알림 관리자를 추가한다. 이미 있으면 False 반환."""
    import json as _json
    admins = get_notification_admins(session)
    if user_id in admins:
        return False
    admins.append(user_id)
    save_bot_setting(session, _NOTIFICATION_ADMINS_KEY, _json.dumps(admins))
    return True


def remove_notification_admin(session: Session, user_id: str) -> bool:
    """알림 관리자를 제거한다. 없으면 False 반환."""
    import json as _json
    admins = get_notification_admins(session)
    if user_id not in admins:
        return False
    admins.remove(user_id)
    save_bot_setting(session, _NOTIFICATION_ADMINS_KEY, _json.dumps(admins))
    return True


# ---------------------------------------------------------------------------
# ProductCategory CRUD
# ---------------------------------------------------------------------------

_DEFAULT_PRODUCTS = [
    {"key": "iruda_backend",  "name": "이루다 백엔드",  "aliases": ["iruda", "이루다", "iruda-backend", "이루다백엔드", "이루다 백엔드"]},
    {"key": "iruda_frontend", "name": "이루다 프론트",  "aliases": ["이루다 프론트", "이루다프론트", "iruda frontend", "이루다 프론트엔드"]},
    {"key": "bizdata",        "name": "비즈데이터",     "aliases": ["bizdata", "비즈데이터", "비즈메타", "biz data", "bizdatav"]},
    {"key": "masterstream",   "name": "마스터스트림",   "aliases": ["masterstream", "마스터스트림", "마스터 스트림", "마스터"]},
    {"key": "metastream",     "name": "메타스트림",     "aliases": ["metastream", "메타스트림", "메타 스트림", "메타"]},
    {"key": "quality",        "name": "퀄리티스트림",   "aliases": ["quality", "qualitystream", "퀄리티", "퀄리티스트림"]},
    {"key": "qtrack",         "name": "큐트랙",         "aliases": ["qtrack", "큐트랙", "q-track"]},
    {"key": "superquery",     "name": "슈퍼쿼리",       "aliases": ["superquery", "슈퍼쿼리", "super-query", "super query"]},
]


def seed_product_categories(session: Session) -> None:
    """제품 카테고리 기본값을 시딩한다. 이미 레코드가 있으면 건너뛴다."""
    import json as _json
    count = session.query(ProductCategory).count()
    if count > 0:
        return
    for p in _DEFAULT_PRODUCTS:
        session.add(ProductCategory(
            product_key=p["key"],
            display_name=p["name"],
            owner_user_ids_json=_json.dumps([], ensure_ascii=False),
            aliases_json=_json.dumps(p["aliases"], ensure_ascii=False),
            question_count=0,
        ))
    session.flush()
    logger.info(f"제품 카테고리 {len(_DEFAULT_PRODUCTS)}개 시딩 완료")


def get_all_product_categories(session: Session) -> list[ProductCategory]:
    """전체 제품 카테고리를 반환한다."""
    return session.query(ProductCategory).order_by(ProductCategory.product_key).all()


def get_product_category(session: Session, product_key: str) -> Optional[ProductCategory]:
    """제품 키로 카테고리를 조회한다."""
    return session.query(ProductCategory).filter(ProductCategory.product_key == product_key).first()


def find_product_by_alias(session: Session, text: str) -> Optional[ProductCategory]:
    """텍스트에서 aliases_json을 비교하여 일치하는 제품을 반환한다."""
    import json as _json
    text_lower = text.lower()
    for cat in session.query(ProductCategory).all():
        aliases = _json.loads(cat.aliases_json or "[]")
        for alias in aliases:
            if alias.lower() == text_lower:
                return cat
    return None


def get_products_as_llm_hints(session: Session) -> list[dict]:
    """LLM 분류에 전달할 제품 힌트 목록을 반환한다."""
    import json as _json
    products = []
    for cat in get_all_product_categories(session):
        products.append({
            "key": cat.product_key,
            "name": cat.display_name,
            "aliases": _json.loads(cat.aliases_json or "[]"),
        })
    return products


def set_product_owners(session: Session, product_key: str, owner_user_ids: list[str]) -> bool:
    """제품 담당자를 설정한다. 제품이 없으면 False 반환."""
    import json as _json
    cat = get_product_category(session, product_key)
    if not cat:
        return False
    cat.owner_user_ids_json = _json.dumps(owner_user_ids, ensure_ascii=False)
    cat.updated_at = datetime.utcnow()
    session.flush()
    return True


def get_product_owners(session: Session, product_key: str) -> list[str]:
    """제품 담당자 목록을 반환한다. 없으면 빈 리스트."""
    import json as _json
    cat = get_product_category(session, product_key)
    if not cat:
        return []
    return _json.loads(cat.owner_user_ids_json or "[]")


def increment_product_question_count(session: Session, product_key: str) -> None:
    """제품의 질문 수를 1 증가시킨다."""
    cat = get_product_category(session, product_key)
    if cat:
        cat.question_count += 1
        cat.updated_at = datetime.utcnow()
        session.flush()


def get_unowned_products_above_threshold(session: Session, threshold: int = 5) -> list[ProductCategory]:
    """질문 수가 임계값 이상이고 담당자가 없는 제품 목록을 반환한다."""
    import json as _json
    candidates = (
        session.query(ProductCategory)
        .filter(ProductCategory.question_count >= threshold)
        .all()
    )
    return [
        c for c in candidates
        if not _json.loads(c.owner_user_ids_json or "[]")
    ]


def mark_product_notified(session: Session, product_key: str) -> None:
    """제품의 알림 발송 시각을 현재 시각으로 갱신한다."""
    cat = get_product_category(session, product_key)
    if cat:
        cat.notified_at = datetime.utcnow()
        cat.updated_at = datetime.utcnow()
        session.flush()


def get_topic_candidates(
    session: Session,
    min_count: int = 5,
    limit: int = 20,
) -> list[dict]:
    """
    미분류 질문이 min_count건 이상 누적된 topic 후보를 반환한다.
    정규화 배치 실행 후 conversation_message.topic이 canonical 이름으로 통일된
    상태를 전제로 한다. product_key가 NULL인 메시지만 집계하여 이미 분류된
    주제는 포함하지 않는다.
    반환: [{"topic": str, "question_count": int, "distinct_channels": int}]
    """
    rows = session.execute(
        text(
            "SELECT topic, "
            "       COUNT(*) AS question_count, "
            "       COUNT(DISTINCT channel_id) AS distinct_channels "
            "FROM conversation_message "
            "WHERE role = 'user' "
            "  AND is_question = true "
            "  AND topic IS NOT NULL "
            "  AND topic NOT IN ('미분류', '없음', '') "
            "  AND product_key IS NULL "
            "GROUP BY topic "
            "HAVING COUNT(*) >= :min_count "
            "ORDER BY question_count DESC "
            "LIMIT :limit"
        ),
        {"min_count": min_count, "limit": limit},
    ).fetchall()

    return [
        {
            "topic": row[0],
            "question_count": row[1],
            "distinct_channels": row[2],
        }
        for row in rows
    ]


def promote_topic_to_product(
    session: Session,
    topic: str,
    product_key: str,
    display_name: str,
) -> bool:
    """
    topic을 정식 ProductCategory로 승격한다.
    이미 product_key가 존재하면 False를 반환한다.
    승격 후 해당 topic을 가진 미분류 메시지에 product_key를 백필한다.
    commit은 호출자가 처리한다.
    """
    existing = get_product_category(session, product_key)
    if existing:
        return False

    session.add(
        ProductCategory(
            product_key=product_key,
            display_name=display_name,
            aliases_json="[]",
            owner_user_ids_json="[]",
            question_count=0,
        )
    )
    session.flush()

    result = session.execute(
        text(
            "UPDATE conversation_message "
            "SET product_key = :key "
            "WHERE topic = :topic "
            "  AND product_key IS NULL"
        ),
        {"key": product_key, "topic": topic},
    )
    backfill_count = result.rowcount
    logger.info(
        f"[promote_topic] topic={topic!r} → product_key={product_key!r}, "
        f"백필 {backfill_count}건"
    )

    cat = get_product_category(session, product_key)
    if cat:
        cat.question_count = backfill_count
        cat.updated_at = datetime.utcnow()
        session.flush()

    return True


# ---------------------------------------------------------------------------
# MessageAttachment CRUD
# ---------------------------------------------------------------------------

def save_attachments(
    session: Session,
    message_id: int,
    attachments: list[AttachmentResult],
) -> None:
    """메시지에 첨부된 파일 분석 결과를 message_attachment 테이블에 저장한다."""
    for att in attachments:
        session.add(MessageAttachment(
            message_id=message_id,
            slack_file_id=att.slack_file_id or None,
            file_name=att.file_name or None,
            mime_type=att.mime_type or None,
            file_type=att.file_type,
            analysis_text=att.analysis_text or None,
        ))
    session.flush()


def get_attachments_for_message(
    session: Session,
    message_id: int,
) -> list[MessageAttachment]:
    """메시지 ID에 연결된 첨부파일 분석 결과 목록을 반환한다."""
    return (
        session.query(MessageAttachment)
        .filter(MessageAttachment.message_id == message_id)
        .order_by(MessageAttachment.id)
        .all()
    )
