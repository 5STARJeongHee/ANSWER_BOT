# topic/is_question/product_key가 NULL인 과거 메시지를 일괄 보정하는 배치 모듈
from __future__ import annotations
import argparse
import logging
import time

from sqlalchemy.orm import Session

from db.models import ConversationMessage, get_session_factory
from db.repository import get_products_as_llm_hints, increment_product_question_count
from services.classifier import classify_message, extract_topic, extract_topic_and_product

logger = logging.getLogger(__name__)

# LLM 호출 간 최소 간격 (rate limit 대응)
_LLM_CALL_INTERVAL = 0.5

# 한 배치에서 처리할 최대 메시지 수 기본값
_DEFAULT_BATCH_SIZE = 200

# N건마다 진행 상황 로그 출력
_PROGRESS_LOG_INTERVAL = 10


def _fetch_unprocessed(session: Session, limit: int) -> list[ConversationMessage]:
    """topic, is_question, 또는 product_key(질문인 경우)가 NULL인 user 메시지를 조회한다."""
    return (
        session.query(ConversationMessage)
        .filter(
            ConversationMessage.role == "user",
            (ConversationMessage.topic == None)  # noqa: E711
            | (ConversationMessage.is_question == None)
            | (
                (ConversationMessage.is_question == True)  # noqa: E712
                & (ConversationMessage.product_key == None)  # noqa: E711
            ),
        )
        .order_by(ConversationMessage.id)
        .limit(limit)
        .all()
    )


def run_categorize_batch(
    session_factory,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """
    topic, is_question, product_key가 NULL인 user 메시지를 보정한다.
    - is_question: LLM 분류기로 actionable 여부 판단
    - topic + product_key: 질문 메시지는 extract_topic_and_product으로 한 번에 추출
    - topic 전용: 비질문 메시지는 extract_topic으로 topic만 추출
    처리 결과 통계 dict를 반환한다.
    """
    stats = {
        "total": 0,
        "is_question_filled": 0,
        "topic_filled": 0,
        "product_key_filled": 0,
        "errors": 0,
    }

    session = session_factory()
    try:
        msgs = _fetch_unprocessed(session, limit=batch_size)
        stats["total"] = len(msgs)

        if not msgs:
            logger.info("보정할 메시지 없음 — 모두 처리된 상태.")
            return stats

        # product_key 분류에 사용할 제품 목록을 루프 전에 한 번만 조회한다.
        products = get_products_as_llm_hints(session)
        logger.info(
            f"미처리 메시지 {len(msgs)}건 보정 시작 (dry_run={dry_run}, 제품 목록 {len(products)}건)"
        )

        for i, msg in enumerate(msgs, 1):
            try:
                did_llm_call = False

                if msg.is_question is None:
                    result = classify_message(msg.content)
                    if not dry_run:
                        msg.is_question = result.is_actionable
                    stats["is_question_filled"] += 1
                    logger.debug(
                        f"is_question: id={msg.id} → {result.is_actionable} "
                        f"(신뢰도={result.confidence:.2f})"
                    )
                    did_llm_call = True
                    time.sleep(_LLM_CALL_INTERVAL)

                needs_topic = msg.topic is None and len(msg.content.strip()) >= 10
                needs_product = msg.is_question is True and msg.product_key is None

                if needs_topic or needs_product:
                    if did_llm_call:
                        time.sleep(_LLM_CALL_INTERVAL)

                    if msg.is_question:
                        # 질문 메시지: topic + product_key를 LLM 1회 호출로 추출
                        topic, product_key = extract_topic_and_product(msg.content, products)
                        if not dry_run:
                            if needs_topic and topic:
                                msg.topic = topic
                                stats["topic_filled"] += 1
                            if needs_product and product_key:
                                msg.product_key = product_key
                                stats["product_key_filled"] += 1
                                # 제품별 질문 카운터 동기화 (백필 데이터 정합성)
                                increment_product_question_count(session, product_key)
                        logger.debug(f"topic+product: id={msg.id} → topic={topic!r}, product={product_key!r}")
                    elif needs_topic:
                        # 비질문 메시지: topic만 추출
                        topic = extract_topic(msg.content)
                        if topic and not dry_run:
                            msg.topic = topic
                            stats["topic_filled"] += 1
                        logger.debug(f"topic: id={msg.id} → {topic!r}")

            except Exception as exc:
                stats["errors"] += 1
                logger.warning(f"메시지 처리 실패 (id={msg.id}): {exc}")

            if i % _PROGRESS_LOG_INTERVAL == 0 or i == len(msgs):
                logger.info(
                    f"[보정 진행] {i}/{len(msgs)}건 | "
                    f"is_question {stats['is_question_filled']}건 | "
                    f"topic {stats['topic_filled']}건 | "
                    f"product_key {stats['product_key_filled']}건 | "
                    f"오류 {stats['errors']}건"
                )

        if not dry_run:
            session.commit()

    except Exception as exc:
        session.rollback()
        logger.error(f"배치 처리 오류: {exc}", exc_info=True)
        raise
    finally:
        session.close()

    logger.info(
        f"[보정 배치 완료] 전체 {stats['total']}건 | "
        f"is_question {stats['is_question_filled']}건 | "
        f"topic {stats['topic_filled']}건 | "
        f"product_key {stats['product_key_filled']}건 | "
        f"오류 {stats['errors']}건"
        + (" (dry_run — DB 미반영)" if dry_run else "")
    )
    return stats


def count_unprocessed(session_factory) -> int:
    """미처리(topic·is_question·product_key 중 하나라도 NULL) user 메시지 수를 반환한다."""
    session = session_factory()
    try:
        return (
            session.query(ConversationMessage)
            .filter(
                ConversationMessage.role == "user",
                (ConversationMessage.topic == None)  # noqa: E711
                | (ConversationMessage.is_question == None)
                | (
                    (ConversationMessage.is_question == True)  # noqa: E712
                    & (ConversationMessage.product_key == None)  # noqa: E711
                ),
            )
            .count()
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 독립 실행 진입점
# 사용법: docker compose exec app python -m batch.categorizer [옵션]
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="미처리 메시지 topic/is_question 일괄 보정")
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"한 번에 처리할 최대 메시지 수 (기본값: {_DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="limit 없이 미처리 메시지 전체를 처리",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="LLM 호출은 하되 DB에 반영하지 않음 (결과 확인용)",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="미처리 메시지 수만 출력하고 종료",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = _parse_args()
    session_factory = get_session_factory()

    if args.count:
        n = count_unprocessed(session_factory)
        print(f"미처리 메시지 수: {n}건")

    elif args.all:
        total_processed = 0
        total_errors = 0
        batch_num = 0
        remaining = count_unprocessed(session_factory)
        print(f"처리 대상: {remaining}건", flush=True)

        while True:
            batch_num += 1
            logger.info(f"[배치 {batch_num}] 시작 (누적 처리 {total_processed}건 완료)")
            stats = run_categorize_batch(
                session_factory,
                batch_size=_DEFAULT_BATCH_SIZE,
                dry_run=args.dry_run,
            )
            total_processed += stats["total"]
            total_errors += stats["errors"]
            logger.info(
                f"[배치 {batch_num} 완료] 이번 {stats['total']}건 | 누적 {total_processed}건"
            )
            if stats["total"] < _DEFAULT_BATCH_SIZE:
                break
            if stats["errors"] == stats["total"]:
                logger.error("모든 메시지 처리 실패 — 반복 중단.")
                break
        print(
            f"전체 처리 완료: {total_processed}건 | 배치 {batch_num}회 | 오류 {total_errors}건 | "
            f"product_key 집계는 로그 참조",
            flush=True,
        )

    else:
        stats = run_categorize_batch(
            session_factory,
            batch_size=args.limit,
            dry_run=args.dry_run,
        )
        print(
            f"처리 완료 — 전체 {stats['total']}건 | "
            f"is_question {stats['is_question_filled']}건 | "
            f"topic {stats['topic_filled']}건 | "
            f"product_key {stats['product_key_filled']}건 | "
            f"오류 {stats['errors']}건"
        )
