# pgvector 유사도 검색 기반 RAG 컨텍스트 검색 서비스
from __future__ import annotations
import logging
from typing import Optional

from sqlalchemy.orm import Session

import config
from db.repository import search_similar_embeddings
from services.llm_service import call_rag_query

logger = logging.getLogger(__name__)

# fastembed 임베딩 모델 (지연 로딩, ONNX 런타임 사용 — torch 불필요)
_embedding_model = None


def _get_embedding_model():
    """임베딩 모델을 지연 로딩한다 (최초 호출 시 ONNX 모델 다운로드)."""
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"임베딩 모델 로딩 중: {config.EMBEDDING_MODEL}")
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding(model_name=config.EMBEDDING_MODEL)
        logger.info("임베딩 모델 로딩 완료")
    return _embedding_model


def embed_text(text: str) -> Optional[list[float]]:
    """텍스트를 벡터로 변환한다. 실패 시 None을 반환한다."""
    if not text or not text.strip():
        return None
    try:
        model = _get_embedding_model()
        vectors = list(model.embed([text]))
        return vectors[0].tolist()
    except Exception as exc:
        logger.error(f"임베딩 생성 실패: {exc}", exc_info=True)
        return None


def _generate_rag_query(question: str) -> str:
    """
    질문에서 검색 쿼리를 LLM으로 생성한다.
    LLM 호출 실패 시 원본 질문을 그대로 사용한다.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "다음 질문에서 핵심 키워드와 의도를 추출하여 "
                "과거 대화 검색에 사용할 검색 쿼리 문장을 1개 생성하라. "
                "한국어로 출력하고 쿼리만 반환한다."
            ),
        },
        {"role": "user", "content": f"질문: {question}"},
    ]
    result = call_rag_query(messages)
    if result and result.strip():
        return result.strip()
    return question


def retrieve_context(
    session: Session,
    question: str,
    channel_id: Optional[str] = None,
    top_k: int = None,
) -> list[dict]:
    """
    질문과 유사한 과거 대화 청크를 검색하여 반환한다.
    반환 형식: [{"chunk_text": str, "similarity": float, "message_id": int}]
    """
    if top_k is None:
        top_k = config.RAG_TOP_K

    # 1. RAG 검색 쿼리 생성
    search_query = _generate_rag_query(question)
    logger.debug(f"RAG 검색 쿼리: {search_query!r}")

    # 2. 쿼리 임베딩 생성
    query_embedding = embed_text(search_query)

    if query_embedding is None and config.ENABLE_VECTOR_SEARCH:
        logger.warning("임베딩 생성 실패, 텍스트 fallback 검색으로 전환")

    # 3. 유사도 검색 (pgvector or fallback)
    try:
        results = search_similar_embeddings(
            session=session,
            query_embedding=query_embedding or [],
            channel_id=channel_id,
            top_k=top_k,
        )
        query_preview = repr(search_query[:50])
        logger.info(f"RAG 검색 완료: {len(results)}건 (쿼리={query_preview})")
        return results
    except Exception as exc:
        logger.error(f"RAG 검색 오류: {exc}", exc_info=True)
        session.rollback()
        return []


def format_context_for_prompt(contexts: list[dict]) -> str:
    """검색된 컨텍스트를 프롬프트 삽입용 문자열로 포맷한다."""
    if not contexts:
        return "(관련 과거 대화 없음)"

    lines = []
    for i, ctx in enumerate(contexts, 1):
        similarity = ctx.get("similarity", 0.0)
        chunk = ctx.get("chunk_text", "").strip()
        if chunk:
            lines.append(f"[{i}] (유사도: {similarity:.2f})\n{chunk}")

    return "\n\n".join(lines) if lines else "(관련 과거 대화 없음)"
