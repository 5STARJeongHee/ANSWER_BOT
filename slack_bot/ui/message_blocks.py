# 답변 유형별 Slack Block Kit 메시지 구성 팩토리
from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

_AI_LABEL_SUFFIX = "[AI 생성 답변]"
_MAX_TEXT_BLOCK_LEN = 3000  # Slack text 블록 최대 길이


def _strip_ai_suffix(text: str) -> str:
    """LLM 프롬프트가 자동 삽입한 '[AI 생성 답변]' 꼬리말을 제거한다."""
    return text.replace(_AI_LABEL_SUFFIX, "").rstrip()


def _truncate(text: str, max_len: int = _MAX_TEXT_BLOCK_LEN) -> str:
    """Slack 블록 텍스트 길이 제한을 초과하면 말줄임표를 붙여 자른다."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _context_footer(context_count: int) -> str:
    """RAG 컨텍스트 수를 기반으로 출처 표시 문자열을 반환한다."""
    if context_count > 0:
        return f"참고한 과거 대화: {context_count}건"
    return "관련 기록 없이 생성된 답변"


# ---------------------------------------------------------------------------
# 로딩(분석 중) 메시지 블록
# ---------------------------------------------------------------------------

def build_thinking_blocks() -> dict:
    """
    '답변 생성 중' 임시 메시지 Block Kit 페이로드를 반환한다.
    chat_postMessage에 **blocks= 로 직접 언팩하여 사용한다.
    """
    return {
        "text": "답변을 생성 중입니다...",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":hourglass_flowing_sand:  *분석 중입니다...*\n잠시만 기다려 주세요.",
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# 일반 답변 메시지 블록
# ---------------------------------------------------------------------------

def build_answer_blocks(
    answer: str,
    context_count: int = 0,
    user_mention: Optional[str] = None,
) -> dict:
    """
    일반 QA 답변 Block Kit 페이로드를 반환한다.

    구성.
    - (선택) 사용자 멘션 헤더
    - 답변 본문
    - 출처/신뢰도 표시 (context 개수 기반)
    - AI 생성 배지 + 피드백 유도 안내
    """
    clean_answer = _strip_ai_suffix(answer)
    footer = _context_footer(context_count)

    blocks: list[dict] = []

    # 1. 헤더 (멘션이 있을 때만)
    if user_mention:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{user_mention} 질문에 대한 답변입니다.",
                },
            }
        )
        blocks.append({"type": "divider"})

    # 2. 답변 본문
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(clean_answer),
            },
        }
    )

    # 3. 출처 + AI 배지 (context 블록)
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":mag: {footer}  |  :robot_face: AI 생성 답변 — 중요한 내용은 담당자에게 확인하세요.",
                }
            ],
        }
    )

    # 4. 피드백 안내
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":thumbsup: / :thumbsdown: 이모지로 답변 품질을 알려주세요.",
                }
            ],
        }
    )

    # plain-text fallback: Slack 알림, 스크린리더에 사용
    plain_text = f"{clean_answer}\n\n[{footer}]"

    return {"text": plain_text, "blocks": blocks}


# ---------------------------------------------------------------------------
# Fallback (불확실) 메시지 블록
# ---------------------------------------------------------------------------

def build_fallback_blocks(
    question: str,
    fallback_user_ids: Optional[list[str]] = None,
) -> dict:
    """
    챗봇이 직접 답변하기 어려운 경우의 담당자 호출 Block Kit 페이로드를 반환한다.

    구성.
    - 경고 아이콘 + 불확실 안내
    - 질문 인용 (최대 200자)
    - 담당자 멘션 또는 안내 CTA 버튼
    """
    short_question = question[:200]

    if fallback_user_ids:
        mention_str = " ".join(f"<@{uid}>" for uid in fallback_user_ids)
        cta_text = f"담당자 {mention_str}에게 문의해 주세요."
    else:
        cta_text = "해당 업무 담당자에게 직접 문의해 주세요."

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":warning:  *정확한 답변을 드리기 어렵습니다.*\n"
                    "해당 질문은 사실 확인이 필요하거나 최신 정보가 부족합니다."
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*질문 내용*\n>>> {_truncate(short_question, 200)}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": cta_text,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":robot_face: AI 챗봇이 자동 생성한 메시지입니다.",
                }
            ],
        },
    ]

    plain_text = (
        f"정확한 답변을 드리기 어렵습니다. {cta_text}\n질문: {short_question}"
    )

    return {"text": plain_text, "blocks": blocks}


# ---------------------------------------------------------------------------
# 에러 메시지 블록
# ---------------------------------------------------------------------------

def build_error_blocks() -> dict:
    """
    시스템 오류 발생 시 표시하는 Block Kit 페이로드를 반환한다.
    """
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":x:  *일시적인 오류가 발생했습니다.*\n"
                    "잠시 후 다시 시도해 주세요. 문제가 지속되면 관리자에게 문의하세요."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":robot_face: AI 챗봇 서비스 — 오류 발생 시 담당자에게 알려주세요.",
                }
            ],
        },
    ]

    return {
        "text": "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# 빈 질문 환영 메시지 블록
# ---------------------------------------------------------------------------

def build_greeting_blocks() -> dict:
    """
    @봇 멘션만 하고 질문 없이 호출했을 때 표시하는 환영 메시지 블록을 반환한다.
    """
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":wave:  *안녕하세요! 사내 Q&A 챗봇입니다.*\n"
                    "업무 관련 궁금한 점을 질문해 주세요. 과거 대화를 바탕으로 답변해 드립니다."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "예시: `@챗봇 연차 신청 방법이 어떻게 되나요?`",
                }
            ],
        },
    ]

    return {
        "text": "안녕하세요! 사내 Q&A 챗봇입니다. 업무 관련 질문을 해주세요.",
        "blocks": blocks,
    }
