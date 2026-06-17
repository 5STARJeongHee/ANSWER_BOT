# 웹 검색 서비스 — DuckDuckGo HTML API를 httpx로 직접 호출하는 provider 구현
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import httpx

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 에러 로그 감지 패턴 — Python/Java/JS 스택 트레이스 및 공통 예외 키워드
# ---------------------------------------------------------------------------
_ERROR_LOG_PATTERN = re.compile(
    r"Traceback|Exception:|Error:|WARN|FATAL|"
    r"at \w+\.\w+\(|NullPointerException|StackOverflow|"
    r"\d{4}-\d{2}-\d{2}.*ERROR|java\.lang\.",
    re.IGNORECASE,
)

# 이미지 분석 블록 마커 (event_handler.py에서 prepend하는 문자열)
_IMAGE_ANALYSIS_MARKER = "[첨부 이미지 분석]"


def should_search(question: str) -> bool:
    """
    웹 검색이 필요한 질문인지 판단한다.
    아래 두 조건 중 하나를 만족하면 True를 반환한다.
      1. 이미지 분석 결과가 포함된 질문 (vision 모델이 분석한 내용 첨부)
      2. 에러 로그나 스택 트레이스가 포함된 기술 질문
    """
    if _IMAGE_ANALYSIS_MARKER in question:
        return True
    if _ERROR_LOG_PATTERN.search(question):
        return True
    return False


def _extract_search_query(question: str) -> str:
    """
    질문 텍스트에서 검색 쿼리를 추출한다.

    이미지 분석 질문: [첨부 이미지 분석] 블록에서 첫 번째 오류 메시지 또는
                      핵심 키워드 라인을 추출한다.
    에러 로그 질문:  첫 번째 Exception/Error 라인을 그대로 사용한다.
    그 외:          원본 질문의 앞 120자를 사용한다 (LLM 호출 없이 처리).

    LLM 추가 호출 없이 처리하는 이유.
    - 이미 vision 분석 텍스트 또는 에러 라인이 좋은 검색어 역할을 한다.
    - free-tier 모델 레이트 리밋 압박을 추가하지 않는다.
    """
    if _IMAGE_ANALYSIS_MARKER in question:
        # [첨부 이미지 분석]\n<분석 텍스트>\n\n<원본 질문> 구조에서 분석 텍스트 첫 줄 추출
        after_marker = question.split(_IMAGE_ANALYSIS_MARKER, 1)[-1].strip()
        first_line = after_marker.splitlines()[0].strip() if after_marker else ""
        if first_line:
            return first_line[:120]

    # 에러 로그: Error:/Exception: 이후 첫 번째 의미 있는 라인 추출
    for line in question.splitlines():
        line = line.strip()
        if _ERROR_LOG_PATTERN.search(line) and len(line) > 5:
            return line[:120]

    # fallback: 원본 질문 앞부분
    return question[:120].strip()


# ---------------------------------------------------------------------------
# Provider 추상화 — 나중에 Tavily, Brave 등으로 교체 가능
# ---------------------------------------------------------------------------

@runtime_checkable
class SearchProvider(Protocol):
    def search(self, query: str, top_k: int, timeout: float) -> list["SearchResult"]:
        """검색 결과 목록을 반환한다. 실패 시 빈 리스트."""
        ...


@dataclass(frozen=True)
class SearchResult:
    title: str
    body: str
    url: str


# ---------------------------------------------------------------------------
# DuckDuckGo HTML API Provider
# ---------------------------------------------------------------------------

class DuckDuckGoProvider:
    """
    DuckDuckGo HTML endpoint(html.duckduckgo.com)를 httpx로 직접 호출한다.
    primp/curl_cffi 같은 컴파일 의존성 없이 python:3.11-slim 이미지에서 동작한다.

    데이터센터 IP에서는 빈 결과를 자주 반환할 수 있다.
    graceful fallback: 빈 결과 시 호출자가 조용히 건너뛴다.
    """

    _SEARCH_URL = "https://html.duckduckgo.com/html/"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    }

    # 검색 결과 블록 파싱 패턴 (DDG HTML 구조 기반)
    _RESULT_PATTERN = re.compile(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    _TAG_PATTERN = re.compile(r"<[^>]+>")
    _ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#x27;": "'"}

    def _strip_html(self, text: str) -> str:
        """HTML 태그와 주요 HTML 엔티티를 제거한다."""
        text = self._TAG_PATTERN.sub("", text)
        for entity, char in self._ENTITY_MAP.items():
            text = text.replace(entity, char)
        return text.strip()

    def search(self, query: str, top_k: int, timeout: float) -> list[SearchResult]:
        """
        DDG HTML 검색을 수행하고 최대 top_k개의 결과를 반환한다.
        네트워크 오류 또는 파싱 실패 시 빈 리스트를 반환한다.
        """
        try:
            response = httpx.post(
                self._SEARCH_URL,
                data={"q": query, "b": "", "kl": "kr-ko"},
                headers=self._HEADERS,
                timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            logger.warning(f"웹 검색 타임아웃 (쿼리={query!r:.50}, timeout={timeout}s)")
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning(f"웹 검색 HTTP 오류: {exc.response.status_code}")
            return []
        except Exception as exc:
            logger.warning(f"웹 검색 실패: {exc}")
            return []

        results: list[SearchResult] = []
        for match in self._RESULT_PATTERN.finditer(response.text):
            if len(results) >= top_k:
                break
            url = match.group(1).strip()
            title = self._strip_html(match.group(2))
            body = self._strip_html(match.group(3))
            if title and body:
                results.append(SearchResult(title=title, body=body, url=url))

        logger.debug(f"DDG 검색 완료: {len(results)}건 (쿼리={query!r:.50})")
        return results


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

# 기본 provider 인스턴스 (모듈 로딩 시 1회 생성)
_default_provider: SearchProvider = DuckDuckGoProvider()


def search_web(question: str, provider: Optional[SearchProvider] = None) -> str:
    """
    질문에 대해 웹 검색을 수행하고 프롬프트 삽입용 텍스트를 반환한다.

    - ENABLE_WEB_SEARCH=false 또는 should_search()=False 시 빈 문자열 반환
    - 검색 실패 또는 결과 없음 시 빈 문자열 반환 (호출자 흐름을 방해하지 않음)

    Args:
        question: 원본 질문 텍스트 (이미지 분석 포함 가능)
        provider:  테스트용 mock provider. None이면 DuckDuckGoProvider 사용

    Returns:
        프롬프트 삽입용 문자열. 결과 없으면 빈 문자열.
    """
    if not config.ENABLE_WEB_SEARCH:
        return ""

    if not should_search(question):
        return ""

    query = _extract_search_query(question)
    if not query:
        return ""

    active_provider = provider or _default_provider

    logger.info(f"웹 검색 시작 (쿼리={query!r:.60})")
    results = active_provider.search(
        query=query,
        top_k=config.WEB_SEARCH_TOP_K,
        timeout=config.WEB_SEARCH_TIMEOUT,
    )

    if not results:
        logger.info("웹 검색 결과 없음 — 기존 컨텍스트로 답변 진행")
        return ""

    max_chars = config.WEB_SEARCH_MAX_CHARS
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        body_trimmed = r.body[:max_chars]
        lines.append(f"[{i}] {r.title}\n{body_trimmed}\n출처: {r.url}")

    formatted = "\n\n".join(lines)
    logger.info(f"웹 검색 완료: {len(results)}건 반환")
    return formatted


def format_web_search_for_prompt(search_text: str) -> str:
    """
    search_web()의 반환값을 프롬프트 블록 형식으로 감싼다.
    빈 문자열 입력 시 빈 문자열을 반환한다 (호출자가 include 여부를 판단).
    """
    if not search_text:
        return ""
    return f"[웹 검색 결과]\n{search_text}"
