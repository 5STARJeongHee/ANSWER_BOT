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
# 자기소개 / 도움말 블록
# ---------------------------------------------------------------------------

def build_intro_blocks() -> dict:
    """
    봇 소개·사용법·지원 기능·명령어를 담은 Block Kit 페이로드를 반환한다.
    자기소개, 사용법, 도움말 질문에 응답할 때 사용한다.
    """
    blocks: list[dict] = [
        # 헤더
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "👋 안녕하세요! 저는 QNA BOT입니다.",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "사내 채널의 대화, 문서, 이미지 등을 학습하여 *업무 관련 질문에 자동으로 답변*하는 AI 챗봇입니다.\n"
                    "과거 대화 기반 RAG(검색 증강 생성) + 웹 검색을 결합해 최대한 정확한 답변을 드립니다."
                ),
            },
        },
        {"type": "divider"},

        # 기본 사용법
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*📌 기본 사용법*\n\n"
                    "• *채널에서 질문*: `@QNA BOT 질문 내용`\n"
                    "• *스레드에서 추가 질문*: 스레드 안에서도 `@QNA BOT 질문 내용`으로 멘션하세요.\n"
                    "• *1:1 DM*: 봇에게 직접 DM을 보내면 멘션 없이 답변합니다."
                ),
            },
        },
        {"type": "divider"},

        # 파일 첨부 지원
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*📎 파일 첨부 지원*\n\n"
                    "질문 시 아래 파일을 함께 첨부하면 내용을 분석해 답변에 반영합니다.\n\n"
                    "| 종류 | 형식 |\n"
                    "|------|------|\n"
                    "| 이미지 | PNG, JPG, GIF, WebP |\n"
                    "| 문서 | TXT, TRF, LOG, CSV, JSON, YAML |\n"
                    "| 스프레드시트 | XLSX, XLS |\n"
                    "| 워드 | DOCX, DOC |\n"
                    "| PDF | PDF |\n"
                    "| 동영상 | MOV, MP4 *(전사 지원 시)* |"
                ),
            },
        },
        {"type": "divider"},

        # 명령어
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*⚙️ 특수 명령어*\n\n"
                    "• *`@QNA BOT 백필 [기간]`* — 채널의 과거 대화를 재수집합니다.\n"
                    "  기간 예시: `7일` `2주` `한달` `3개월` `90` *(숫자는 일 수)*\n"
                    "  기간 생략 시 기본 90일 기준으로 실행됩니다.\n\n"
                    "• *`@QNA BOT 소개`* / *`@QNA BOT 도움말`* — 이 안내를 다시 표시합니다."
                ),
            },
        },
        {"type": "divider"},

        # 답변 품질 & 한계
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*⚠️ 알아두세요*\n\n"
                    "• 답변은 과거 대화·문서 기반 AI 생성 결과입니다. 중요한 내용은 담당자에게 재확인하세요.\n"
                    "• 정확한 답변을 모를 경우 *'확인이 필요합니다'* 라고 답변하고 담당자를 호출합니다.\n"
                    "• :thumbsup: / :thumbsdown: 이모지로 답변 품질을 피드백해 주시면 개선에 도움이 됩니다."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":robot_face:  RAG + Hybrid Search + Cross-Encoder Reranking 기반 사내 AI 챗봇",
                }
            ],
        },
    ]

    return {
        "text": "안녕하세요! QNA BOT입니다. 사내 채널 대화를 학습하여 업무 질문에 자동 답변합니다. '@QNA BOT 질문'으로 멘션해 주세요.",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# 빈 질문 환영 메시지 블록
# ---------------------------------------------------------------------------

def build_greeting_blocks() -> dict:
    """
    @봇 멘션만 하고 질문 없이 호출했을 때 표시하는 환영·사용법 안내 블록을 반환한다.
    """
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":wave:  *안녕하세요! 사내 Q&A 챗봇입니다.*",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*:bulb: 사용 방법*\n\n"
                    "*1. 채널에서 질문*\n"
                    "`@QNA_BOT 질문 내용`\n\n"
                    "*2. 스레드에서 추가 질문*\n"
                    "스레드 안에서도 `@QNA_BOT 질문 내용` 으로 멘션하세요.\n\n"
                    "*3. 이미지 첨부*\n"
                    "에러 화면이나 설정 캡처를 이미지로 첨부하면 텍스트를 추출·분석해 드립니다."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        ":robot_face:  과거 대화를 기반으로 답변합니다. "
                        "모르는 내용은 *'확인이 필요합니다'* 라고 답변합니다. "
                        "직접 `@QNA_BOT` 을 멘션해야만 답변합니다."
                    ),
                }
            ],
        },
    ]

    return {
        "text": "안녕하세요! 사내 Q&A 챗봇입니다. @QNA_BOT 질문 내용 형식으로 멘션해 주세요.",
        "blocks": blocks,
    }
