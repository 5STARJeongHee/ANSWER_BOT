# 과거 Slack 대화 이력을 일괄 수집(백필)하는 배치 모듈
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

from slack_sdk import WebClient

import config
from db.repository import upsert_message, save_embedding, get_last_message_ts
from services.context_retriever import embed_text
from utils.image_processor import analyze_slack_files
from utils.pii_filter import apply_pii_filter

logger = logging.getLogger(__name__)

# Slack conversations.history API rate limit: 50회/분
# 1.3초 간격으로 요청하여 여유 있게 처리
_API_CALL_INTERVAL = 1.3
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
) -> int:
    """
    지정 채널의 최근 N일 대화를 수집하여 DB에 저장한다.
    이미 수집된 메시지는 중복 없이 건너뜀.
    수집된 신규 메시지 수를 반환한다.
    """
    # DB에 이미 수집된 메시지가 있으면 마지막 ts 이후만 가져온다 (증분 수집).
    # 없으면 최근 N일치 전체를 가져온다 (최초 수집).
    fallback_dt = datetime.utcnow() - timedelta(days=days)
    session = session_factory()
    try:
        last_ts = get_last_message_ts(session, channel_id)
    finally:
        session.close()

    if last_ts:
        # 마지막 ts와 정확히 같은 메시지는 이미 있으므로 미세하게 이후 시점부터 요청
        oldest_ts = str(float(last_ts) + 0.000001)
        oldest_label = f"마지막 수집 이후 (ts={last_ts})"
    else:
        oldest_ts = _datetime_to_slack_ts(fallback_dt)
        oldest_label = f"최근 {days}일 (oldest={fallback_dt.strftime('%Y-%m-%d')})"

    logger.info(f"백필 시작 (channel={channel_id}, 범위={oldest_label})")

    collected_count = 0
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
            if cursor:
                kwargs["cursor"] = cursor

            response = client.conversations_history(**kwargs)

            if not response.get("ok"):
                logger.error(f"API 오류 (channel={channel_id}): {response.get('error')}")
                break

            messages = response.get("messages", [])
            if not messages:
                break

            # subtype 제외, 텍스트 또는 파일이 있는 메시지만 처리
            valid_msgs = [
                msg for msg in messages
                if msg.get("subtype") not in ("bot_message", "channel_join", "channel_leave")
                and (msg.get("text", "").strip() or msg.get("files"))
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

                # Thread 단위 청킹: 이 페이지에 등장한 스레드 답글의 thread_ts를 수집
                if config.ENABLE_THREAD_CHUNKING:
                    from db.repository import save_thread_chunk_embedding
                    thread_tss = {
                        msg.get("thread_ts")
                        for msg in valid_msgs
                        if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts")
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
                logger.debug(f"페이지 {page} 처리 완료 (누적={collected_count}건)")

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

    logger.info(f"백필 완료 (channel={channel_id}, 신규 수집={collected_count}건)")
    return collected_count


def run_all_channels_backfill(client: WebClient, session_factory, days: int = _BACKFILL_DAYS) -> None:
    """
    모든 대상 채널의 백필을 순차 실행한다.
    최초 배포 시 수동 또는 배치로 1회 실행한다.
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
        )
        total += count
        # 채널 간 대기 (rate limit 분산)
        time.sleep(2)

    logger.info(f"전체 백필 완료: 총 {total}건 수집")
