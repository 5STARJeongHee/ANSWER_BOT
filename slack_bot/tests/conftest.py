# pytest 공통 fixture 및 경로 설정
import sys
import os

# slack_bot 패키지 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 환경변수를 테스트 실행 전에 설정하여 config 임포트 시 기본값이 적용되도록 한다.
# config.py는 모듈 임포트 시점에 os.getenv로 읽으므로, 임포트 전에 미리 설정해야 한다.
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("ENABLE_VECTOR_SEARCH", "false")

import pytest


@pytest.fixture(autouse=True)
def clear_classifier_cache():
    """각 테스트 전후 classifier 모듈 레벨 캐시를 초기화하여 테스트 간 오염을 방지한다."""
    import services.classifier as classifier_module
    classifier_module._classify_cache.clear()
    yield
    classifier_module._classify_cache.clear()
