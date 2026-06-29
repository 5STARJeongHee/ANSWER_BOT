# topic 정규화 배치 — Stage 2: LLM 그룹핑으로 자유 텍스트 topic을 canonical 주제명으로 통합
from __future__ import annotations
import argparse
import json
import logging
import threading
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

import config
from db.models import get_session_factory
from services.llm_service import call_with_fallback, parse_json_response

logger = logging.getLogger(__name__)

# 동시 실행 방지 플래그
_normalize_running = threading.Event()

_NORMALIZE_SYSTEM_PROMPT = (
    "너는 사내 Slack 대화에서 추출된 topic 태그를 정규화하는 전문가다.\n"
    "입력으로 주어진 topic 목록(각 topic과 출현 빈도)을 의미적으로 동일한 그룹으로 묶어라.\n\n"
    "규칙:\n"
    "- 같은 의미의 표현을 하나의 canonical 이름으로 통합한다.\n"
    "- canonical 이름은 가장 간결하고 명확한 한국어 2~5단어로 정한다.\n"
    "- 의미가 다른 topic은 별도 그룹으로 유지한다.\n"
    "- 유사도가 불명확한 경우 별도 그룹으로 두는 편이 낫다.\n"
    "- 그룹이 1개 멤버뿐이어도 canonical을 지정한다 (표현 정제 목적).\n\n"
    '출력 형식 (JSON만):\n{"groups": [{"canonical": "Redis 연결 오류", "members": ["레디스 접속 실패", "Redis 연결 안 됨"]}]}'
)


def _fetch_distinct_topics(session: Session) -> dict[str, int]:
    """topic IS NOT NULL인 user 메시지의 distinct topic과 빈도를 반환한다."""
    rows = session.execute(
        text(
            "SELECT topic, COUNT(*) AS n "
            "FROM conversation_message "
            "WHERE topic IS NOT NULL AND role = 'user' "
            "GROUP BY topic "
            "ORDER BY n DESC"
        )
    ).fetchall()
    return {row[0]: row[1] for row in rows}


_NORMALIZE_CHUNK_SIZE = 80


def _group_chunk_by_llm(chunk: list[dict]) -> dict[str, str]:
    """
    topic 청크(최대 _NORMALIZE_CHUNK_SIZE개)를 LLM으로 그룹핑하고
    {raw_topic: canonical_topic} 맵을 반환한다.
    """
    user_content = (
        f"다음 {len(chunk)}개의 topic을 의미별로 그룹핑하고 canonical 이름을 정해줘.\n\n"
        "topic 목록 (topic: 출현 횟수):\n"
        + json.dumps(chunk, ensure_ascii=False, indent=2)
    )
    messages = [
        {"role": "system", "content": _NORMALIZE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    # 청크당 최소 80토큰 * 청크 크기 확보 (JSON 구조 오버헤드 포함)
    max_tokens = max(4000, len(chunk) * 80)

    raw = call_with_fallback(
        model_chain=config.SUMMARY_FALLBACK_CHAIN,
        messages=messages,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    if not raw:
        return {}

    parsed = parse_json_response(raw, default={"groups": []})
    groups = parsed.get("groups", [])
    if not isinstance(groups, list):
        return {}

    result: dict[str, str] = {}
    for group in groups:
        canonical = str(group.get("canonical", "")).strip()
        members = group.get("members", [])
        if not canonical or not isinstance(members, list):
            continue
        for member in members:
            raw_topic = str(member).strip()
            if raw_topic:
                result[raw_topic] = canonical
    return result


def _group_topics_by_llm(topic_counts: dict[str, int]) -> dict[str, str]:
    """
    distinct topic 목록을 _NORMALIZE_CHUNK_SIZE 단위 청크로 나눠 LLM에 전달하고
    {raw_topic: canonical_topic} 맵을 반환한다.
    LLM 실패 또는 파싱 실패 시 빈 dict를 반환한다.
    """
    if not topic_counts:
        return {}

    topic_list = [{"topic": t, "count": n} for t, n in topic_counts.items()]
    chunks = [
        topic_list[i: i + _NORMALIZE_CHUNK_SIZE]
        for i in range(0, len(topic_list), _NORMALIZE_CHUNK_SIZE)
    ]
    logger.info(
        f"LLM 그룹핑 시작: {len(topic_counts)}개 topic → "
        f"{len(chunks)}개 청크 (청크당 최대 {_NORMALIZE_CHUNK_SIZE}개)"
    )

    canonical_map: dict[str, str] = {}
    failed_chunks = 0
    for idx, chunk in enumerate(chunks, 1):
        chunk_result = _group_chunk_by_llm(chunk)
        if not chunk_result:
            logger.warning(f"청크 {idx}/{len(chunks)} 그룹핑 실패 또는 빈 응답")
            failed_chunks += 1
        else:
            canonical_map.update(chunk_result)
            logger.debug(f"청크 {idx}/{len(chunks)} 완료: 매핑 {len(chunk_result)}건 추가")

    total_groups = len({v for v in canonical_map.values()})
    logger.info(
        f"LLM 그룹핑 완료: {len(topic_counts)}개 distinct topic → "
        f"{total_groups}개 그룹, 매핑 {len(canonical_map)}건 "
        f"(실패 청크 {failed_chunks}/{len(chunks)})"
    )
    return canonical_map


def _apply_canonical_map(
    session: Session,
    canonical_map: dict[str, str],
    dry_run: bool = False,
) -> int:
    """
    canonical_map 기준으로 conversation_message.topic을 일괄 UPDATE한다.
    변경된 총 행 수를 반환한다.
    """
    total_updated = 0
    for raw_topic, canonical in canonical_map.items():
        if raw_topic == canonical:
            continue
        if not dry_run:
            result = session.execute(
                text(
                    "UPDATE conversation_message "
                    "SET topic = :canonical "
                    "WHERE topic = :raw AND role = 'user'"
                ),
                {"canonical": canonical, "raw": raw_topic},
            )
            total_updated += result.rowcount
        else:
            logger.debug(f"[dry-run] {raw_topic!r} → {canonical!r}")
            total_updated += 1
    return total_updated


def run_normalize_batch(
    session_factory,
    dry_run: bool = False,
) -> dict:
    """
    topic 정규화 배치를 실행한다.
    - distinct topic 목록을 LLM으로 그룹핑
    - canonical 맵으로 conversation_message.topic 일괄 UPDATE
    통계 dict를 반환한다.
    """
    stats = {
        "distinct_topics": 0,
        "groups_formed": 0,
        "rows_updated": 0,
        "errors": 0,
    }

    session = session_factory()
    try:
        topic_counts = _fetch_distinct_topics(session)
        stats["distinct_topics"] = len(topic_counts)

        if not topic_counts:
            logger.info("정규화할 topic 없음 — topic IS NOT NULL인 메시지가 없습니다.")
            return stats

        logger.info(f"정규화 시작: distinct topic {len(topic_counts)}개 (dry_run={dry_run})")

        canonical_map = _group_topics_by_llm(topic_counts)
        if not canonical_map:
            stats["errors"] += 1
            logger.error("LLM 그룹핑 결과 없음 (전 청크 실패) — 정규화를 중단합니다.")
            return stats

        # LLM이 반환한 raw가 실제 DB topic에 있는 것만 유효
        valid_map = {
            raw: can
            for raw, can in canonical_map.items()
            if raw in topic_counts
        }
        stats["groups_formed"] = len({v for v in valid_map.values()})

        rows_updated = _apply_canonical_map(session, valid_map, dry_run=dry_run)
        stats["rows_updated"] = rows_updated

        if not dry_run:
            session.commit()

    except Exception as exc:
        session.rollback()
        stats["errors"] += 1
        logger.error(f"정규화 배치 오류: {exc}", exc_info=True)
        raise
    finally:
        session.close()

    logger.info(
        f"[정규화 완료] distinct {stats['distinct_topics']}개 → "
        f"그룹 {stats['groups_formed']}개 | 행 업데이트 {stats['rows_updated']}건"
        + (" (dry_run — DB 미반영)" if dry_run else "")
    )
    return stats


def count_distinct_topics(session_factory) -> int:
    """topic IS NOT NULL인 user 메시지의 distinct topic 수를 반환한다."""
    session = session_factory()
    try:
        row = session.execute(
            text(
                "SELECT COUNT(DISTINCT topic) FROM conversation_message "
                "WHERE topic IS NOT NULL AND role = 'user'"
            )
        ).fetchone()
        return row[0] if row else 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 독립 실행 진입점
# 사용법: docker compose exec app python -m batch.topic_normalizer [옵션]
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="topic 정규화 배치 (Stage 2)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="LLM 그룹핑은 수행하되 DB에 반영하지 않음 (결과 확인용)",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="distinct topic 수만 출력하고 종료",
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
        n = count_distinct_topics(session_factory)
        print(f"distinct topic 수: {n}개")
    else:
        stats = run_normalize_batch(session_factory, dry_run=args.dry_run)
        print(
            f"처리 완료 — distinct {stats['distinct_topics']}개 | "
            f"그룹 {stats['groups_formed']}개 | "
            f"행 업데이트 {stats['rows_updated']}건 | "
            f"오류 {stats['errors']}건"
            + (" (dry_run)" if args.dry_run else "")
        )
