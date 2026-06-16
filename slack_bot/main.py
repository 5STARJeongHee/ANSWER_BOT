# 사내 Slack Q&A 챗봇 애플리케이션 진입점
import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config

# ---------------------------------------------------------------------------
# 로깅 초기화 (가장 먼저 설정)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    """앱 초기화 및 기동 메인 함수."""

    # 1. 환경변수 유효성 검증
    try:
        config.validate_config()
    except EnvironmentError as exc:
        logger.critical(f"환경변수 설정 오류로 종료: {exc}")
        sys.exit(1)

    # 2. DB 엔진 및 세션 팩토리 초기화
    logger.info("데이터베이스 초기화 중...")
    from db.models import get_engine, get_session_factory, init_db
    try:
        engine = get_engine()
        init_db(engine)
        session_factory = get_session_factory(engine)
        logger.info("데이터베이스 초기화 완료")
    except Exception as exc:
        logger.critical(f"DB 초기화 실패: {exc}", exc_info=True)
        sys.exit(1)

    # 3. Bolt 앱 초기화 (Socket Mode)
    app = App(
        token=config.SLACK_BOT_TOKEN,
        signing_secret=config.SLACK_SIGNING_SECRET,
    )

    # 4. 이벤트 핸들러 등록
    from handlers.event_handler import register_handlers
    register_handlers(app=app, session_factory=session_factory)
    logger.info("이벤트 핸들러 등록 완료")

    # 5. APScheduler 시작
    from batch.scheduler import create_scheduler
    scheduler = create_scheduler(session_factory=session_factory)
    scheduler.start()
    logger.info("배치 스케줄러 시작 완료")

    # 6. 백필 실행 여부 확인 (최초 배포 시 환경변수로 트리거)
    import os
    if os.getenv("RUN_BACKFILL", "false").lower() == "true":
        logger.info("백필 배치 실행 시작...")
        from batch.collector import run_all_channels_backfill
        from slack_sdk import WebClient
        backfill_client = WebClient(token=config.SLACK_BOT_TOKEN)
        try:
            run_all_channels_backfill(
                client=backfill_client,
                session_factory=session_factory,
            )
        except Exception as exc:
            logger.error(f"백필 오류 (앱 기동은 계속): {exc}", exc_info=True)

    # 7. Socket Mode 핸들러로 앱 기동
    logger.info("Slack Socket Mode 핸들러 시작...")
    try:
        handler = SocketModeHandler(app=app, app_token=config.SLACK_APP_TOKEN)
        handler.start()
    except KeyboardInterrupt:
        logger.info("사용자 인터럽트로 종료")
    finally:
        scheduler.shutdown(wait=False)
        logger.info("앱 종료 완료")


if __name__ == "__main__":
    main()
