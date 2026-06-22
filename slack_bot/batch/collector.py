# 과거 Slack 대화 이력을 일괄 수집(백필)하는 배치 모듈
from __future__ import annotations
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

from slack_sdk import WebClient

import config
from db.repository import upsert_message, save_embedding, get_oldest_message_ts
from services.context_retriever import embed_text
from utils.image_processor import analyze_slack_files
from utils.pii_filter import apply_pii_filter

logger = logging.getLogger(__name__)

# Slack conversations.history API rate limit: 50회/분
# 1.3초 간격으로 요청하여 여유 있게 처리
_API_CALL_INTERVAL = 1.3

# ---------------------------------------------------------------------------
# 봇 명령어 메시지 필터
# ---------------------------------------------------------------------------
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>", re.IGNORECASE)
_BACKFILL_CMD_RE = re.compile(
    r"^(백필|backfill|재수집)"
    r"(\s+(\d+일|\d+주일?|\d+개월|\d+달|한달|두달|세달|일주일|오늘|전체|all|\d+))?$",
    re.IGNORECASE,
)
_INTRO_CMD_RE = re.compile(r"^(도움말|help|소개|명령어|사용법)$", re.IGNORECASE)


def _is_bot_command_message(text: str) -> bool:
    """봇 멘션 제거 후 봇 명령어 전용 메시지이면 True를 반환한다."""
    cleaned = _MENTION_RE.sub("", text).strip()
    if not cleaned:
        return True  # 멘션만 있는 빈 메시지
    return bool(_BACKFILL_CMD_RE.match(cleaned)) or bool(_INTRO_CMD_RE.match(cleaned))


_BACKFILL_DAYS = 90
# 이미지 다운로드 병렬 워커 수 (vision은 세마포어로 직렬화됨)
_BACKFILL_IMAGE_WORKERS = 3


def _enrich_with_images(msg: dict, bot_token: str) -> str:
    """메시지 텍스트에 이미지 분석 결과를 앞에 붙인 최종 내용을 반환한다."""
    raw_text = msg.get("text", "")
    files = msg.get("files") or []
    if not files:
        return raw_text
    image_ctx = analyze_slack_files(files, bot_token)
    if not image_ctx:
        return raw_text
    if raw_text.strip():
        return f"[첨부 이미지 분석]\n{image_ctx}\n\n{raw_text}".strip()
    return f"[첨부 이미지 분석]\n{image_ctx}"


def _slack_ts_to_datetime(ts: str) -> datetime:
    """Slack 타임스탬프(UNIX epoch 소수점)를 datetime으로 변환한다."""
    return datetime.utcfromtimestamp(float(ts.split(".")[0]))


def _datetime_to_slack_ts(dt: datetime) -> str:
    """datetime을 Slack API oldest 파라미터용 timestamp 문자열로 변환한다."""
    return str(dt.timestamp())


def backfill_channel(
    client: WebClient,
    session_factory,
    channel_id: str,
    days: int = _BACKFILL_DAYS,
    force: bool = False,
) -> int:
    """
    지정 채널의 최근 N일 대화를 수집하여 DB에 저장한다.
    - force=False(기본): 이미 수집된 메시지는 건너뜀. 아직 수집 안 된 과거 구간만 채운다.
    - force=True: 조기 종료 없이 전체 기간을 재수집하고 기존 메시지를 갱신한다.
    수집된 신규/갱신 메시지 수를 반환한다.
    """
    fallback_dt = datetime.utcnow() - timedelta(days=days)
    oldest_ts = _datetime_to_slack_ts(fallback_dt)

    session = session_factory()
    try:
        oldest_db_ts = get_oldest_message_ts(session, channel_id)
    finally:
        session.close()

    if not force:
        # 일반 모드: DB 최솟값이 이미 N일 전보다 오래됨 → 수집할 과거 구간 없음
        if oldest_db_ts and float(oldest_db_ts) <= float(oldest_ts):
            logger.info(
                f"백필 스킵 — 이미 {days}일치 모두 수집됨 (channel={channel_id}, "
                f"db_oldest={_slack_ts_to_datetime(oldest_db_ts).strftime('%Y-%m-%d %H:%M')})"
            )
            return 0

    if not force and oldest_db_ts:
        # 일반 모드: DB에 데이터가 있으면 그 직전까지만 수집
        latest_ts: Optional[str] = str(float(oldest_db_ts) - 0.000001)
        oldest_label = (
            f"최근 {days}일 ~ "
            f"{_slack_ts_to_datetime(oldest_db_ts).strftime('%Y-%m-%d %H:%M')} 이전"
        )
    else:
        # force 모드 또는 DB 비어있음: 전체 기간 수집
        latest_ts = None
        oldest_label = (
            f"최근 {days}일 전체 재수집 (force)"
            if force
            else f"최근 {days}일 전체 (oldest={fallback_dt.strftime('%Y-%m-%d')})"
        )

    logger.info(f"백필 시작 (channel={channel_id}, 범위={oldest_label})")

    collected_count = 0
    fetched_total = 0   # API에서 가져온 누적 valid 메시지 수
    dup_count = 0       # 이미 DB에 있어 스킵된 누적 수
    cursor = None
    page = 0

    while True:
        page += 1
        try:
            kwargs: dict = {
                "channel": channel_id,
                "oldest": oldest_ts,
                "limit": 200,
            }
            if latest_ts:
                kwargs["latest"] = latest_ts
            if cursor:
                kwargs["cursor"] = cursor

            response = client.conversations_history(**kwargs)

            if not response.get("ok"):
                logger.error(f"API 오류 (channel={channel_id}): {response.get('error')}")
                break

            messages = response.get("messages", [])
            if not messages:
                break

            # subtype 제외, 텍스트 또는 파일이 있는 메시지만 처리, 봇 명령어 제외
            valid_msgs = [
                msg for msg in messages
                if msg.get("subtype") not in ("bot_message", "channel_join", "channel_leave")
                and (msg.get("text", "").strip() or msg.get("files"))
                and not _is_bot_command_message(msg.get("text", ""))
            ]

            # 이미지 다운로드를 병렬로 수행하고 vision 분석 결과를 텍스트에 붙인다.
            # (vision 호출 자체는 세마포어로 직렬화되어 QA와 경쟁하지 않음)
            enriched: dict[int, str] = {}
            if valid_msgs:
                with ThreadPoolExecutor(max_workers=_BACKFILL_IMAGE_WORKERS) as executor:
                    future_to_idx = {
                        executor.submit(_enrich_with_images, msg, config.SLACK_BOT_TOKEN): i
                        for i, msg in enumerate(valid_msgs)
                    }
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        try:
                            enriched[idx] = future.result()
                        except Exception as exc:
                            logger.warning(f"메시지 이미지 분석 실패 (page={page}, idx={idx}): {exc}")
                            enriched[idx] = valid_msgs[idx].get("text", "")
                logger.debug(f"페이지 {page} 이미지 분석 완료 ({len(valid_msgs)}건)")

            # DB 저장은 순차 처리 (세션 공유)
            fetched_total += len(valid_msgs)
            page_new = 0
            page_dup = 0
            session = session_factory()
            try:
                for i, msg in enumerate(valid_msgs):
                    ts = msg.get("ts", "")
                    user_id = msg.get("user")
                    bot_id = msg.get("bot_id")
                    role = "bot" if bot_id else "user"
                    content = enriched.get(i, msg.get("text", ""))
                    clean_content = apply_pii_filter(content)

                    if not clean_content.strip():
                        continue

                    saved = upsert_message(
                        session=session,
                        event_id=None,
                        channel_id=channel_id,
                        thread_ts=msg.get("thread_ts"),
                        message_ts=ts,
                        user_id=user_id,
                        role=role,
                        content=clean_content,
                        force=force,
                    )

                    if saved:
                        embedding = embed_text(clean_content)
                        save_embedding(
                            session=session,
                            source_message_id=saved.id,
                            chunk_text=clean_content,
                            embedding=embedding,
                        )
                        collected_count += 1
                        page_new += 1
                    else:
                        dup_count += 1
                        page_dup += 1

                # Thread 단위 청킹: conversations.history는 스레드 부모만 반환하므로
                # thread_ts == ts인 부모도 포함해야 DB에 저장된 봇 답변과 함께 청크를 생성할 수 있다.
                if config.ENABLE_THREAD_CHUNKING:
                    from db.repository import save_thread_chunk_embedding
                    thread_tss = {
                        msg.get("thread_ts")
                        for msg in valid_msgs
                        if msg.get("thread_ts")
                    }
                    for ts in thread_tss:
                        try:
                            save_thread_chunk_embedding(
                                session=session,
                                channel_id=channel_id,
                                thread_ts=ts,
                                embed_fn=embed_text,
                            )
                        except Exception as exc:
                            logger.warning(f"Thread 청크 저장 실패 (ts={ts}): {exc}")

                session.commit()
                has_next = bool(response.get("response_metadata", {}).get("next_cursor"))
                logger.info(
                    f"[백필 진행] 채널={channel_id} | 페이지 {page}"
                    f" | 이번 {len(valid_msgs)}건 → 신규 {page_new}건 저장 / 중복 {page_dup}건 스킵"
                    f" | 누적 저장 {collected_count}건 / 가져옴 {fetched_total}건"
                    + (" | 다음 페이지 있음" if has_next else " | 마지막 페이지")
                )

            except Exception as exc:
                session.rollback()
                logger.error(f"메시지 저장 오류 (page={page}): {exc}", exc_info=True)
            finally:
                session.close()

            # 다음 페이지 커서
            meta = response.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor:
                break

            # Rate limit 대응
            time.sleep(_API_CALL_INTERVAL)

        except Exception as exc:
            logger.error(f"백필 오류 (channel={channel_id}, page={page}): {exc}", exc_info=True)
            # 일시적 오류 시 잠시 대기 후 재시도
            time.sleep(5)
            # 연속 오류 방지를 위해 루프 종료
            break

    logger.info(
        f"[백필 완료] 채널={channel_id} | 페이지 {page}개 | "
        f"총 가져옴 {fetched_total}건 | 신규 저장 {collected_count}건 | 중복 스킵 {dup_count}건"
    )
    return collected_count


def run_all_channels_backfill(
    client: WebClient,
    session_factory,
    days: int = _BACKFILL_DAYS,
    force: bool = False,
) -> None:
    """
    모든 대상 채널의 백필을 순차 실행한다.
    최초 배포 시 수동 또는 배치로 1회 실행한다.
    force=True이면 이미 수집된 데이터도 재수집한다.
    """
    if not config.TARGET_CHANNEL_IDS:
        logger.warning("백필 대상 채널이 없음. TARGET_CHANNEL_IDS를 설정하세요.")
        return

    total = 0
    for channel_id in config.TARGET_CHANNEL_IDS:
        count = backfill_channel(
            client=client,
            session_factory=session_factory,
            channel_id=channel_id,
            days=days,
            force=force,
        )
        total += count
        # 채널 간 대기 (rate limit 분산)
        time.sleep(2)

    logger.info(f"전체 백필 완료: 총 {total}건 수집{'(강제 재수집)' if force else ''}")
