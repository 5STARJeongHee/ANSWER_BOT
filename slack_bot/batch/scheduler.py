# APScheduler 기반 배치 작업 등록 및 관리 모듈
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config

logger = logging.getLogger(__name__)


def _run_weekly_summary_job(session_factory) -> None:
    """주간 요약 배치 작업 래퍼 (APScheduler에서 호출)."""
    from services.summarizer import run_weekly_summary
    session = session_factory()
    try:
        run_weekly_summary(session=session)
    except Exception as exc:
        logger.error(f"주간 요약 배치 오류: {exc}", exc_info=True)
    finally:
        session.close()


def create_scheduler(session_factory) -> BackgroundScheduler:
    """
    APScheduler 인스턴스를 생성하고 배치 작업을 등록한다.
    start()는 main.py에서 호출한다.
    """
    scheduler = BackgroundScheduler(
        job_defaults={
            "coalesce": True,       # 지연 실행이 누적됐을 때 1회만 실행
            "max_instances": 1,     # 동시 실행 인스턴스 제한
            "misfire_grace_time": 60 * 10,  # 10분 이내 지연은 실행 허용
        },
        timezone="Asia/Seoul",
    )

    # 주간 요약 배치 (월요일 새벽 2시 - 설정 파일로 변경 가능)
    scheduler.add_job(
        func=_run_weekly_summary_job,
        trigger=CronTrigger(
            day_of_week=config.SUMMARY_BATCH_WEEKDAY,
            hour=config.SUMMARY_BATCH_HOUR,
            minute=0,
        ),
        args=[session_factory],
        id="weekly_summary",
        name="주간 대화 요약 배치",
        replace_existing=True,
    )

    logger.info(
        f"스케줄러 작업 등록 완료: weekly_summary "
        f"(day_of_week={config.SUMMARY_BATCH_WEEKDAY}, hour={config.SUMMARY_BATCH_HOUR})"
    )

    return scheduler
