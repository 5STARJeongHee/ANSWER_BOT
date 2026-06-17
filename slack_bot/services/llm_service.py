# LLM 서비스 통합 모듈 — OpenRouter / Ollama 공통 재시도·fallback 로직
from __future__ import annotations
import json
import logging
import random
import re
import time
from typing import Optional

from openai import OpenAI, RateLimitError, APIError, APITimeoutError

import config

logger = logging.getLogger(__name__)

# OpenAI 호환 클라이언트 — LLM_BACKEND에 따라 OpenRouter 또는 Ollama에 연결
_client = OpenAI(
    api_key=config.LLM_API_KEY,
    base_url=config.LLM_BASE_URL,
    timeout=config.LLM_TIMEOUT,
)

# JSON 마크다운 펜스 제거 패턴 (Gemma 등의 모델이 ```json ... ``` 래핑)
_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _strip_json_fence(text: str) -> str:
    """응답 텍스트에서 마크다운 코드 펜스를 제거한다."""
    match = _JSON_FENCE_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _call_with_retry(
    model: str,
    messages: list[dict],
    max_tokens: int,
    response_format: Optional[dict] = None,
    max_retries: int = None,
) -> Optional[str]:
    """
    단일 모델로 API를 호출하고 지수 백오프로 재시도한다.
    성공 시 응답 텍스트를 반환하고, 모든 재시도 실패 시 None을 반환한다.
    """
    if max_retries is None:
        max_retries = config.MAX_RETRIES

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    for attempt in range(max_retries):
        try:
            response = _client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if content is None:
                # reasoning-only 모드 모델 (nvidia nemotron nano 등)
                logger.warning(f"모델 {model} 응답 content=None (reasoning-only 모드)")
                return None
            return content

        except RateLimitError as exc:
            if attempt == max_retries - 1:
                logger.error(f"Rate limit 초과 (모델: {model}, 재시도 소진): {exc}")
                return None
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Rate limit 429 (모델: {model}, {wait:.1f}초 후 재시도, attempt={attempt + 1})")
            time.sleep(wait)

        except (APITimeoutError, APIError) as exc:
            if attempt == max_retries - 1:
                logger.error(f"API 오류 (모델: {model}, 재시도 소진): {exc}")
                return None
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"API 오류 (모델: {model}, {wait:.1f}초 후 재시도): {exc}")
            time.sleep(wait)

        except Exception as exc:
            logger.error(f"예상치 못한 오류 (모델: {model}): {exc}", exc_info=True)
            return None

    return None


def call_with_fallback(
    model_chain: list[str],
    messages: list[dict],
    max_tokens: int,
    response_format: Optional[dict] = None,
) -> Optional[str]:
    """
    fallback 체인 순서대로 모델을 시도하고 첫 성공 응답을 반환한다.
    모든 모델 실패 시 None을 반환한다.
    """
    for model in model_chain:
        result = _call_with_retry(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        if result is not None:
            if model != model_chain[0]:
                logger.info(f"Fallback 모델 사용: {model}")
            return result
        logger.warning(f"모델 실패, 다음 fallback으로: {model}")

    return None


def call_classifier(messages: list[dict]) -> Optional[str]:
    """분류기 모델을 호출한다. JSON 응답 강제."""
    return call_with_fallback(
        model_chain=config.CLASSIFIER_FALLBACK_CHAIN,
        messages=messages,
        max_tokens=config.CLASSIFIER_MAX_TOKENS,
        response_format={"type": "json_object"},
    )


def call_qa(messages: list[dict]) -> Optional[str]:
    """QA 답변 모델을 호출한다."""
    return call_with_fallback(
        model_chain=config.QA_FALLBACK_CHAIN,
        messages=messages,
        max_tokens=config.QA_MAX_TOKENS,
    )


def call_summary(messages: list[dict]) -> Optional[str]:
    """요약 모델을 호출한다."""
    return call_with_fallback(
        model_chain=config.SUMMARY_FALLBACK_CHAIN,
        messages=messages,
        max_tokens=config.SUMMARY_MAX_TOKENS,
    )


def call_rag_query(messages: list[dict]) -> Optional[str]:
    """RAG 검색 쿼리 생성 모델을 호출한다."""
    return call_with_fallback(
        model_chain=config.RAG_QUERY_FALLBACK_CHAIN,
        messages=messages,
        max_tokens=100,
    )


def call_vision(image_b64: str, prompt: str) -> Optional[str]:
    """압축된 이미지(base64)와 프롬프트를 vision 모델에 전달하고 응답을 반환한다."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                },
            ],
        }
    ]
    return _call_with_retry(
        model=config.IMAGE_MODEL,
        messages=messages,
        max_tokens=600,
    )


def parse_json_response(raw: str, default: dict) -> dict:
    """
    LLM 응답에서 JSON을 파싱한다.
    마크다운 펜스, 불완전한 JSON 등을 허용하며 파싱 실패 시 default를 반환한다.
    """
    if not raw:
        return default

    cleaned = _strip_json_fence(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"JSON 파싱 실패 (원문: {raw[:200]!r})")
        return default
