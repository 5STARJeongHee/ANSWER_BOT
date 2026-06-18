# Hybrid Search + Reranking 기반 Advanced RAG 컨텍스트 검색 서비스
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
# fastembed TextCrossEncoder 재정렬 모델 (지연 로딩, 실패 시 영구 비활성화)
_reranker_model = None
_reranker_unavailable = False


def _get_embedding_model():
    """임베딩 모델을 지연 로딩한다 (최초 호출 시 ONNX 모델 다운로드)."""
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"임베딩 모델 로딩 중: {config.EMBEDDING_MODEL}")
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding(model_name=config.EMBEDDING_MODEL)
        logger.info("임베딩 모델 로딩 완료")
    return _embedding_model


def _get_reranker():
    """Cross-Encoder 재정렬 모델을 지연 로딩한다. 실패 시 None을 반환하고 이후 호출을 건너뛴다."""
    global _reranker_model, _reranker_unavailable
    if _reranker_unavailable:
        return None
    if _reranker_model is None:
        try:
            from fastembed import TextCrossEncoder
            _reranker_model = TextCrossEncoder(model_name=config.RERANK_MODEL)
            logger.info(f"Reranker 모델 로딩 완료: {config.RERANK_MODEL}")
        except Exception as exc:
            logger.warning(f"Reranker 로딩 실패 (reranking 비활성화): {exc}")
            _reranker_unavailable = True
    return _reranker_model


def _rerank_contexts(query: str, contexts: list[dict]) -> list[dict]:
    """
    Cross-Encoder로 컨텍스트를 재정렬한다.
    Reranker 로딩 실패 또는 ENABLE_RERANKING=false이면 원본 순서를 유지한다.
    """
    if not config.ENABLE_RERANKING or not contexts:
        return contexts
    reranker = _get_reranker()
    if not reranker:
        return contexts
    try:
        passages = [ctx["chunk_text"] for ctx in contexts]
        scores = list(reranker.rerank(query, passages))
        ranked = sorted(zip(scores, contexts), key=lambda x: x[0], reverse=True)
        return [ctx for _, ctx in ranked]
    except Exception as exc:
        logger.warning(f"Reranking 실패, 원본 순서 유지: {exc}")
        return contexts


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


def _generate_rag_query(question: str, thread_summary: Optional[str] = None) -> str:
    """
    질문(과 필요시 스레드 요약)에서 핵심 키워드와 의도를 추출하여
    과거 대화 검색에 사용할 검색 쿼리 문장을 1개 생성한다.
    """
    system_content = (
        "다음 질문에서 핵심 키워드와 의도를 추출하여 "
        "과거 대화 검색에 사용할 검색 쿼리 문장을 1개 생성하라. "
        "한국어로 출력하고 쿼리만 반환한다."
    )
    user_content = f"질문: {question}"
    if thread_summary:
        user_content = f"[스레드 이전 문맥]\n{thread_summary}\n\n질문: {question}"

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
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
    thread_summary: Optional[str] = None,
    image_context: Optional[str] = None,
) -> list[dict]:
    """
    질문과 유사한 과거 대화 청크를 검색하여 반환한다.
    ENABLE_RERANKING=true이면 pool_k 후보를 가져온 뒤 Cross-Encoder로 재정렬 후 top_k를 반환한다.
    image_context가 있으면 이미지 추출 텍스트로 추가 검색하고 결과를 병합한다.
    반환 형식: [{"chunk_text": str, "similarity": float, "message_id": int, "role": str, "chunk_type": str}]
    """
    if top_k is None:
        top_k = config.RAG_TOP_K

    # 이미지 포함 질문이면 쿼리 변환 손실을 감안해 낮은 임계값 적용
    similarity_threshold = (
        config.RAG_IMAGE_SIMILARITY_THRESHOLD if image_context
        else config.RAG_SIMILARITY_THRESHOLD
    )

    # Reranking 활성화 시 초기 후보를 더 많이 가져온다
    pool_k = config.RAG_RERANK_POOL_K if config.ENABLE_RERANKING else top_k

    # 1. RAG 검색 쿼리 생성
    search_query = _generate_rag_query(question, thread_summary)
    logger.debug(f"RAG 검색 쿼리: {search_query!r}")

    # 2. 쿼리 임베딩 생성
    query_embedding = embed_text(search_query)

    if query_embedding is None and config.ENABLE_VECTOR_SEARCH:
        logger.warning("임베딩 생성 실패, 텍스트 fallback 검색으로 전환")

    # 3. 유사도 검색 (hybrid or vector or fallback)
    try:
        results = search_similar_embeddings(
            session=session,
            query_embedding=query_embedding or [],
            query_text=search_query,
            channel_id=channel_id,
            top_k=pool_k,
        )
        # 유사도 임계값 미만 청크 제거 (fallback 결과는 similarity=0.0이므로 제외 안 함)
        filtered = [
            r for r in results
            if r["similarity"] == 0.0 or r["similarity"] >= similarity_threshold
        ]

        # 3b. 이미지 텍스트로 추가 직접 검색 후 병합 (LLM 쿼리 변환 없이 원문 사용)
        if image_context and image_context.strip():
            image_embedding = embed_text(image_context)
            image_results = search_similar_embeddings(
                session=session,
                query_embedding=image_embedding or [],
                query_text=image_context,
                channel_id=channel_id,
                top_k=pool_k,
            )
            existing_ids = {r.get("message_id") for r in filtered}
            for r in image_results:
                if (r["similarity"] == 0.0 or r["similarity"] >= similarity_threshold) and (
                    r.get("message_id") not in existing_ids
                ):
                    filtered.append(r)
                    existing_ids.add(r.get("message_id"))

        # 4. Cross-Encoder 재정렬 후 상위 top_k 반환
        if config.ENABLE_RERANKING and len(filtered) > top_k:
            filtered = _rerank_contexts(search_query, filtered)

        final = filtered[:top_k]

        query_preview = repr(search_query[:50])
        logger.info(
            f"RAG 검색 완료: {len(final)}건 / 후보 {len(results)}건 "
            f"(임계값={similarity_threshold}, rerank={'on' if config.ENABLE_RERANKING else 'off'}, "
            f"이미지검색={'on' if image_context else 'off'}, 쿼리={query_preview})"
        )
        return final
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
        chunk_type = ctx.get("chunk_type", "message")
        role = ctx.get("role", "")

        if chunk_type == "thread":
            role_label = "[스레드 Q&A]"
        elif role == "user":
            role_label = "[사람 답변]"
        elif role == "bot":
            role_label = "[봇 답변]"
        else:
            role_label = ""

        if chunk:
            max_chars = config.THREAD_CHUNK_MAX_CHARS if chunk_type == "thread" else config.RAG_CHUNK_MAX_CHARS
            chunk = chunk[:max_chars]
            header = f"[{i}] (유사도: {similarity:.2f})"
            if role_label:
                header += f" {role_label}"
            lines.append(f"{header}\n{chunk}")

    return "\n\n".join(lines) if lines else "(관련 과거 대화 없음)"
