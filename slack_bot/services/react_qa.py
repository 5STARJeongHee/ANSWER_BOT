# ReAct(Reasoning + Acting) 패턴 QA 엔진 — LLM이 다단계 검색을 스스로 결정
from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from typing import Optional

import config

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<(search_rag|search_web|answer)>([\s\S]*?)</\1>", re.IGNORECASE)

_REACT_SYSTEM_PROMPT = """\
당신은 사내 업무 지원 Slack 챗봇이다. 단계적으로 정보를 수집해 정확한 답변을 제공한다.
매 응답에서 다음 중 하나만 출력하라.

- <search_rag>검색 쿼리</search_rag>  : 사내 과거 대화/문서 추가 검색
- <search_web>검색 쿼리</search_web>  : 인터넷 검색 (최신 정보 필요 시)
- <answer>최종 답변</answer>           : 충분한 정보를 확보했을 때 최종 답변 출력

지침:
- 현재 컨텍스트만으로 답할 수 있으면 즉시 <answer>로 마무리하라.
- 검색 쿼리는 한국어로, 핵심 키워드 중심으로 작성하라.
- 최종 답변 끝에 '[AI 생성 답변]'을 덧붙인다.\
"""

_FORCE_ANSWER_PROMPT = (
    "지금까지 수집한 정보만으로 즉시 <answer>최종 답변</answer> 형식으로 마무리하라. "
    "<search_rag>, <search_web> 태그는 더 이상 사용하지 마라."
)


@dataclass(frozen=True)
class ReactResult:
    answer: str
    rag_contexts: list
    used_web_search: bool
    iterations: int


def _parse_action(text: str) -> tuple[str, str]:
    """LLM 응답에서 액션 태그를 파싱한다. 태그 없으면 전체를 answer로 처리한다."""
    m = _TAG_RE.search(text)
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return "answer", text.strip()


def _build_initial_user_message(
    question: str,
    thread_summary: Optional[str],
    image_context: Optional[str],
    initial_rag_text: str,
    initial_web_text: str,
) -> str:
    parts = []
    if image_context:
        parts.append(f"[첨부파일 분석]\n{image_context}")
    if thread_summary:
        parts.append(f"[스레드 문맥 요약]\n{thread_summary}")
    if initial_rag_text:
        parts.append(f"[사내 검색 결과]\n{initial_rag_text}")
    if initial_web_text:
        parts.append(f"[웹 검색 결과]\n{initial_web_text}")
    parts.append(f"[질문]\n{question}")
    return "\n\n".join(parts)


def react_qa(
    session,
    question: str,
    channel_id: Optional[str],
    thread_summary: Optional[str] = None,
    image_context: Optional[str] = None,
    topic: Optional[str] = None,
    max_iterations: int = 3,
) -> ReactResult:
    """
    ReAct 루프로 QA를 처리한다.
    LLM이 search_rag / search_web / answer 중 하나를 선택하며 최대 max_iterations번 반복한다.
    """
    from services.context_retriever import retrieve_context, format_context_for_prompt
    from services.web_search import search_web, format_web_search_for_prompt
    from services.llm_service import call_qa
    from utils.token_counter import trim_messages_to_budget

    rag_contexts: list = []
    used_web_search = False

    # 초기 RAG 검색
    initial_ctx = retrieve_context(
        session, question,
        channel_id=channel_id,
        thread_summary=thread_summary,
        image_context=image_context,
        topic=topic,
    )
    rag_contexts.extend(initial_ctx)
    initial_rag_text = format_context_for_prompt(initial_ctx)

    # 초기 웹 검색 (ENABLE_WEB_SEARCH=true 일 때)
    initial_web_text = ""
    if config.ENABLE_WEB_SEARCH:
        raw_web = search_web(question)
        if raw_web:
            initial_web_text = format_web_search_for_prompt(raw_web)
            used_web_search = True

    messages: list[dict] = [{"role": "system", "content": _REACT_SYSTEM_PROMPT}]
    messages.append({
        "role": "user",
        "content": _build_initial_user_message(
            question, thread_summary, image_context, initial_rag_text, initial_web_text
        ),
    })

    for i in range(max_iterations):
        if i == max_iterations - 1:
            # 마지막 iteration: 강제 최종 답변 요청
            messages.append({"role": "user", "content": _FORCE_ANSWER_PROMPT})

        trimmed = trim_messages_to_budget(
            messages, _REACT_SYSTEM_PROMPT, config.MAX_CONTEXT_TOKENS
        )
        response = call_qa(trimmed)
        if response is None:
            logger.warning(f"ReAct iteration {i+1}: call_qa 응답 없음")
            break

        action, value = _parse_action(response)
        logger.debug(f"ReAct iteration {i+1}: action={action!r} value={value[:80]!r}")

        if action == "answer":
            return ReactResult(
                answer=value,
                rag_contexts=rag_contexts,
                used_web_search=used_web_search,
                iterations=i + 1,
            )

        messages.append({"role": "assistant", "content": response})

        if action == "search_rag":
            new_ctx = retrieve_context(session, value, channel_id=channel_id)
            rag_contexts.extend(new_ctx)
            obs = format_context_for_prompt(new_ctx) if new_ctx else "관련 사내 정보를 찾지 못했습니다."
            messages.append({"role": "user", "content": f"[RAG 검색 결과]\n{obs}"})

        elif action == "search_web":
            raw = search_web(value)
            obs = format_web_search_for_prompt(raw) if raw else "관련 웹 정보를 찾지 못했습니다."
            if raw:
                used_web_search = True
            messages.append({"role": "user", "content": f"[웹 검색 결과]\n{obs}"})

    return ReactResult(
        answer="",
        rag_contexts=rag_contexts,
        used_web_search=used_web_search,
        iterations=max_iterations,
    )
