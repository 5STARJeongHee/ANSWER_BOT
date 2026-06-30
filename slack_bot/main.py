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

    # 1-1. 모니터링 메트릭 서버 시작
    try:
        from prometheus_client import start_http_server
        start_http_server(config.METRICS_PORT)
        logger.info(f"Prometheus 메트릭 서버 시작 완료 (포트: {config.METRICS_PORT})")
    except Exception as exc:
        logger.error(f"메트릭 서버 시작 실패: {exc}")

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

    # 봇 user_id 사전 취득 — 이벤트/리액션 핸들러가 공유한다.
    try:
        bot_user_id: str = app.client.auth_test()["user_id"]
        logger.info(f"봇 user_id 확인: {bot_user_id}")
    except Exception as exc:
        logger.warning(f"auth_test 실패, bot_user_id 없이 동작: {exc}")
        bot_user_id = None

    # 4. 이벤트 핸들러 등록
    from handlers.event_handler import register_handlers
    register_handlers(app=app, session_factory=session_factory, bot_user_id=bot_user_id)
    logger.info("이벤트 핸들러 등록 완료")

    # 4-1. 리액션 핸들러 등록 (👍/👎 피드백 수집, reactions:read 스코프 필요)
    from ui.reaction_handler import register_reaction_handlers, _VOTE_BUTTONS
    register_reaction_handlers(app=app, session_factory=session_factory, bot_user_id=bot_user_id)
    logger.info("리액션 핸들러 등록 완료")

    # 4-2. 부정 피드백 투표 버튼 핸들러 등록 (block_actions)
    _vote_action_map = {action_id: value for action_id, value, _ in _VOTE_BUTTONS}

    def _make_vote_handler(reason_value: str):
        def handle_feedback_vote(ack, body, action, respond) -> None:
            ack()
            voter_id: str = body["user"]["id"]
            original_ts: str = action.get("value", "")
            channel: str = (body.get("channel") or {}).get("id", "")

            if not original_ts or not channel:
                return

            from db.repository import update_feedback_failure_reason
            session = session_factory()
            try:
                update_feedback_failure_reason(
                    session,
                    message_ts=original_ts,
                    user_id=voter_id,
                    user_reason=reason_value,
                )
                session.commit()
                logger.info(f"사용자 피드백 원인 저장: user={voter_id} reason={reason_value} ts={original_ts}")
            except Exception as exc:
                session.rollback()
                logger.error(f"사용자 피드백 원인 저장 실패: {exc}", exc_info=True)
            finally:
                session.close()

            try:
                respond(
                    replace_original=True,
                    text="소중한 피드백 감사합니다.",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "✅ 소중한 피드백 감사합니다. 더 좋은 품질의 응답에 도움이 됩니다.",
                            },
                        }
                    ],
                )
            except Exception as exc:
                logger.warning(f"피드백 메시지 업데이트 실패: {exc}")
        return handle_feedback_vote

    for _action_id, _reason_value, _ in _VOTE_BUTTONS:
        app.action(_action_id)(_make_vote_handler(_reason_value))
    logger.info("부정 피드백 투표 핸들러 등록 완료")

    # 5. APScheduler 시작
    from batch.scheduler import create_scheduler
    scheduler = create_scheduler(session_factory=session_factory)
    scheduler.start()
    logger.info("배치 스케줄러 시작 완료")

    # 6. 백필 실행 여부 확인 (최초 배포 시 환경변수로 트리거)
    # 데몬 스레드로 실행하여 Socket Mode 핸들러 기동을 블로킹하지 않는다.
    import os
    import threading
    if os.getenv("RUN_BACKFILL", "false").lower() == "true":
        from batch.collector import run_all_channels_backfill
        from slack_sdk import WebClient
        backfill_client = WebClient(token=config.SLACK_BOT_TOKEN)

        def _run_backfill():
            logger.info("백필 배치 실행 시작 (백그라운드 스레드)...")
            try:
                run_all_channels_backfill(
                    client=backfill_client,
                    session_factory=session_factory,
                )
            except Exception as exc:
                logger.error(f"백필 오류: {exc}", exc_info=True)
            logger.info("백필 배치 완료")

        threading.Thread(target=_run_backfill, daemon=True, name="backfill").start()

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
