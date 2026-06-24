# APScheduler 기반 배치 작업 등록 및 관리 모듈
from __future__ import annotations
import json
import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config

logger = logging.getLogger(__name__)

# 실행 중인 스케줄러 인스턴스 (event_handler에서 update_summary_schedule 호출 시 사용)
_scheduler: Optional[BackgroundScheduler] = None

SETTING_KEY = "summary_schedule"

# 주기 타입별 기본 요약 기간 (일 수)
_PERIOD_DAYS_BY_TYPE = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
}


def _run_summary_job(session_factory, period_days: int) -> None:
    """요약 배치 작업 래퍼 (APScheduler에서 호출)."""
    from services.summarizer import run_summary_batch
    session = session_factory()
    try:
        run_summary_batch(session=session, period_days=period_days)
    except Exception as exc:
        logger.error(f"요약 배치 오류: {exc}", exc_info=True)
    finally:
        session.close()


def _run_categorize_job(session_factory) -> None:
    """topic·is_question 보정 배치 작업 래퍼 (APScheduler에서 호출)."""
    from batch.categorizer import run_categorize_batch
    try:
        run_categorize_batch(session_factory)
    except Exception as exc:
        logger.error(f"보정 배치 오류: {exc}", exc_info=True)


def _run_normalize_job(session_factory) -> None:
    """topic 정규화 배치 작업 래퍼 (APScheduler에서 호출)."""
    from batch.topic_normalizer import run_normalize_batch
    try:
        run_normalize_batch(session_factory)
    except Exception as exc:
        logger.error(f"정규화 배치 오류: {exc}", exc_info=True)


def _make_trigger(cfg: dict) -> CronTrigger:
    """schedule config dict로 CronTrigger를 생성한다."""
    t = cfg.get("type", "weekly")
    hour = int(cfg.get("hour", config.SUMMARY_BATCH_HOUR))
    if t == "daily":
        return CronTrigger(hour=hour, minute=0, timezone="Asia/Seoul")
    if t == "monthly":
        day = int(cfg.get("day", 1))
        return CronTrigger(day=day, hour=hour, minute=0, timezone="Asia/Seoul")
    # weekly (기본)
    weekday = int(cfg.get("weekday", config.SUMMARY_BATCH_WEEKDAY))
    return CronTrigger(day_of_week=weekday, hour=hour, minute=0, timezone="Asia/Seoul")


def _describe_schedule(cfg: dict) -> str:
    """설정을 사람이 읽을 수 있는 한국어로 변환한다."""
    t = cfg.get("type", "weekly")
    hour = int(cfg.get("hour", 2))
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    if t == "daily":
        return f"매일 오전 {hour}시"
    if t == "monthly":
        day = int(cfg.get("day", 1))
        return f"매월 {day}일 오전 {hour}시"
    weekday = int(cfg.get("weekday", 0))
    return f"매주 {weekday_names[weekday]}요일 오전 {hour}시"


def update_summary_schedule(session_factory, cfg: dict) -> str:
    """
    요약 배치 주기를 변경하고 DB에 저장한다.
    cfg 예: {"type": "daily", "hour": 3}
             {"type": "weekly", "weekday": 0, "hour": 2}
             {"type": "monthly", "day": 1, "hour": 2}
    변경 결과 설명 문자열을 반환한다.
    """
    global _scheduler
    if _scheduler is None:
        return "스케줄러가 초기화되지 않았습니다."

    period_days = _PERIOD_DAYS_BY_TYPE.get(cfg.get("type", "weekly"), 7)
    trigger = _make_trigger(cfg)

    _scheduler.reschedule_job(job_id="summary", trigger=trigger)
    _scheduler.modify_job(job_id="summary", args=[session_factory, period_days])

    # DB 저장 (재시작 후에도 복원)
    from db.repository import save_bot_setting
    session = session_factory()
    try:
        save_bot_setting(session, SETTING_KEY, json.dumps(cfg, ensure_ascii=False))
        session.commit()
    finally:
        session.close()

    desc = _describe_schedule(cfg)
    logger.info(f"요약 배치 주기 변경: {desc}")
    return desc


def get_current_schedule_description(session_factory) -> str:
    """현재 저장된 요약 배치 주기 설명을 반환한다."""
    from db.repository import get_bot_setting
    session = session_factory()
    try:
        raw = get_bot_setting(session, SETTING_KEY)
    finally:
        session.close()

    if raw:
        try:
            cfg = json.loads(raw)
            return _describe_schedule(cfg)
        except Exception:
            pass

    # DB에 없으면 환경변수 기본값
    default_cfg = {
        "type": "weekly",
        "weekday": config.SUMMARY_BATCH_WEEKDAY,
        "hour": config.SUMMARY_BATCH_HOUR,
    }
    return _describe_schedule(default_cfg)


def create_scheduler(session_factory) -> BackgroundScheduler:
    """
    APScheduler 인스턴스를 생성하고 배치 작업을 등록한다.
    DB에 저장된 주기 설정이 있으면 우선 적용한다.
    start()는 main.py에서 호출한다.
    """
    global _scheduler

    scheduler = BackgroundScheduler(
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60 * 10,
        },
        timezone="Asia/Seoul",
    )

    # DB 저장 설정 우선 적용, 없으면 환경변수 기본값
    from db.repository import get_bot_setting
    session = session_factory()
    try:
        raw = get_bot_setting(session, SETTING_KEY)
    finally:
        session.close()

    cfg: dict = {}
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception:
            pass

    if not cfg:
        cfg = {
            "type": "weekly",
            "weekday": config.SUMMARY_BATCH_WEEKDAY,
            "hour": config.SUMMARY_BATCH_HOUR,
        }

    period_days = _PERIOD_DAYS_BY_TYPE.get(cfg.get("type", "weekly"), 7)
    trigger = _make_trigger(cfg)

    scheduler.add_job(
        func=_run_summary_job,
        trigger=trigger,
        args=[session_factory, period_days],
        id="summary",
        name="대화 요약 배치",
        replace_existing=True,
    )

    # 미분류 메시지 카테고리/주제 채우기 — 매일 새벽 4시 (요약 배치 2시간 후)
    scheduler.add_job(
        func=_run_categorize_job,
        trigger=CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"),
        args=[session_factory],
        id="categorize",
        name="topic·is_question 보정 배치",
        replace_existing=True,
    )

    # topic 정규화 — 매일 새벽 3시 (보정 배치보다 1시간 앞서 실행)
    scheduler.add_job(
        func=_run_normalize_job,
        trigger=CronTrigger(hour=3, minute=0, timezone="Asia/Seoul"),
        args=[session_factory],
        id="normalize",
        name="topic 정규화 배치",
        replace_existing=True,
    )

    _scheduler = scheduler
    logger.info(
        f"스케줄러 작업 등록 완료: summary ({_describe_schedule(cfg)}), "
        "topic 정규화 (매일 03:00), topic·is_question 보정 (매일 04:00)"
    )
    return scheduler
