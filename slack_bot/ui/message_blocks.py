# 답변 유형별 Slack Block Kit 메시지 구성 팩토리
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional


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
    show_thread_tip: bool = False,
    related_qa: Optional[list[dict]] = None,
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

    # 5. 스레드 사용 팁 (채널 최초 질문 시에만)
    if show_thread_tip:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            ":thread: 이 답변에 *스레드로 후속 질문*하시면 대화 맥락이 누적되어 "
                            "다음 답변이 더 정확해집니다."
                        ),
                    }
                ],
            }
        )

    # 6. 같은 주제의 과거 Q&A 블록 추가
    if related_qa:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "🏷️ *같은 주제의 과거 Q&A*",
                },
            }
        )
        qa_lines = []
        for qa in related_qa:
            qa_lines.append(f"• *Q:* {qa['q_preview']}\n  *A:* {qa['a_preview']}")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n\n".join(qa_lines),
                },
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
                    "• *`@QNA BOT 히스토리`* — 이 채널의 최근 질문·대화 이력을 보여줍니다.\n\n"
                    "• *`@QNA BOT 대시보드 [기간]`* — 봇 응답 통계 대시보드를 표시합니다.\n"
                    "  기간 예시: `7일` `한달` *(기간 생략 시 기본 7일)*\n\n"
                    "• *`@QNA BOT 정규화 실행`* — 누적된 topic 태그를 LLM으로 그룹핑해 canonical 이름으로 통합합니다. *(관리자)*\n\n"
                    "• *`@QNA BOT 요약 주기 설정 [주기]`* — 대화 요약 배치 주기를 변경합니다.\n"
                    "  예시: `매일 3시` `매주 월요일 2시` `매월 1일 2시`\n\n"
                    "• *`@QNA BOT 요약 주기 확인`* — 현재 설정된 요약 배치 주기를 확인합니다.\n\n"
                    "• *`@QNA BOT 담당자 목록`* — 제품별 담당자 현황을 확인합니다.\n\n"
                    "• *`@QNA BOT 담당자 설정 [제품키] @담당자1 @담당자2`* — 제품 담당자를 지정합니다.\n"
                    "  예시: `담당자 설정 iruda_backend @홍길동 @김철수`\n\n"
                    "• *`@QNA BOT 담당자 삭제 [제품키]`* — 제품 담당자를 초기화합니다.\n\n"
                    "• *`@QNA BOT 알림관리자 목록`* — 담당자 지정 요청 알림을 받는 관리자 목록을 확인합니다.\n\n"
                    "• *`@QNA BOT 알림관리자 추가 @관리자1 @관리자2`* — 알림 관리자를 추가합니다.\n\n"
                    "• *`@QNA BOT 알림관리자 삭제 @관리자`* — 알림 관리자를 제거합니다.\n\n"
                    "• *`@QNA BOT 소개`* / *`@QNA BOT 도움말`* — 이 안내를 다시 표시합니다."
                ),
            },
        },
        {"type": "divider"},

        # 더 정확한 답변을 위한 팁
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*:thread: 더 정확한 답변을 원하신다면*\n\n"
                    "• *스레드로 대화*하세요. 봇 답변에 스레드로 후속 질문을 달면 대화 흐름이 "
                    "하나의 맥락으로 누적되어 이후 유사한 질문에도 더 정확한 답변이 제공됩니다.\n"
                    "• *동료의 답변도 학습*됩니다. 스레드 안에서 동료가 직접 답변을 달면 "
                    "그 내용이 봇의 지식으로 쌓여 다음 답변 품질이 높아집니다."
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
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        ":information_source:  백필·파일 첨부 등 전체 기능은 "
                        "`@QNA_BOT 도움말` 을 입력하면 확인할 수 있습니다."
                    ),
                }
            ],
        },
    ]

    return {
        "text": "안녕하세요! 사내 Q&A 챗봇입니다. @QNA_BOT 질문 내용 형식으로 멘션해 주세요.",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# 질문 이력 블록
# ---------------------------------------------------------------------------

_THREADS_PER_DAY_LIMIT = 10


def build_history_blocks(
    grouped: dict[date, list[dict]],
    channel_name: str = "이 채널",
    days: int = 7,
) -> dict:
    """
    날짜별·스레드별 대화 이력을 Block Kit 페이로드로 반환한다.
    grouped: get_channel_history_by_date() 반환값 (날짜 내림차순 OrderedDict).
    """
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 {channel_name} 대화 이력",
                "emoji": True,
            },
        },
    ]

    if not grouped:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_아직 저장된 대화가 없습니다. `@QNA BOT 질문내용` 으로 질문해 보세요!_",
                },
            }
        )
        return {"text": f"{channel_name} 대화 이력이 없습니다.", "blocks": blocks}

    total_threads = sum(len(v) for v in grouped.values())

    for day, threads in grouped.items():
        day_str = day.strftime("%Y.%m.%d")
        day_count = len(threads)

        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*━━━ {day_str} (대화 {day_count}건) ━━━*",
                    }
                ],
            }
        )
        blocks.append({"type": "divider"})

        visible = threads[:_THREADS_PER_DAY_LIMIT]
        overflow = day_count - len(visible)

        lines = []
        for t in visible:
            topic_tag = f"`{t['topic']}`" if t["topic"] else "(주제 미분류)"
            authors = " ".join(f"<@{uid}>" for uid in t["user_ids"]) if t["user_ids"] else "익명"
            msg_count = t["message_count"]
            entry = f"• {topic_tag} {authors} · 메시지 {msg_count}개"
            if t.get("q_preview"):
                entry += f"\n  _Q: {t['q_preview']}_"
            if t.get("a_preview"):
                entry += f"\n  _A: {t['a_preview']}_"
            lines.append(entry)

        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        )

        if overflow > 0:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"_+{overflow}건 더 있음_"}
                    ],
                }
            )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":information_source: 최근 {days}일 · 총 {total_threads}건",
                }
            ],
        }
    )

    return {"text": f"{channel_name} 최근 {days}일 대화 이력 {total_threads}건", "blocks": blocks}


# ---------------------------------------------------------------------------
# 주제별 대화 이력 블록
# ---------------------------------------------------------------------------

def build_history_grouped_blocks(
    topic_groups: list[dict],
    total_count: int,
    channel_name: str = "이 채널",
    days: int = 7,
) -> dict:
    """
    topic별로 묶인 대화 이력을 Block Kit 페이로드로 반환한다.
    topic_groups: get_channel_history_by_topic() 반환값.
    """
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 {channel_name} 주제별 대화 이력",
                "emoji": True,
            },
        },
    ]

    if not topic_groups:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_아직 저장된 질문이 없습니다. `@QNA BOT 질문내용` 으로 질문해 보세요!_",
                },
            }
        )
        return {"text": f"{channel_name} 주제별 대화 이력", "blocks": blocks}

    all_unclassified = all(g["topic"] == "미분류" for g in topic_groups)
    if all_unclassified:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":bulb: 주제 분류가 아직 실행되지 않았습니다. `@QNA BOT 정규화 실행` 으로 주제를 분류할 수 있습니다.",
                    }
                ],
            }
        )

    for group in topic_groups:
        topic: str = group["topic"]
        count: int = group["count"]
        entries: list[dict] = group["entries"]

        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*🏷️ {topic}* — {count}건",
                    }
                ],
            }
        )

        lines: list[str] = []
        for entry in entries:
            created_at = entry.get("created_at")
            day_str = created_at.strftime("%m.%d") if created_at else "??"
            user_id = entry.get("user_id")
            user_mention = f"<@{user_id}>" if user_id else "익명"
            q_preview = entry.get("q_preview", "")
            a_preview = entry.get("a_preview")

            line = f"• {day_str} {user_mention}\n  _Q: {q_preview}_"
            if a_preview:
                line += f"\n  _A: {a_preview}_"
            lines.append(line)

        if lines:
            section_text = "\n".join(lines)
            if len(section_text) > 2900:
                section_text = section_text[:2900] + "…"
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": section_text},
                }
            )

        overflow = count - len(entries)
        if overflow > 0:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"_+{overflow}건 더 있음_"}
                    ],
                }
            )

    topic_count = len(topic_groups)
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":information_source: 최근 {days}일 · 총 {total_count}건 · {topic_count}개 주제",
                }
            ],
        }
    )

    return {
        "text": f"{channel_name} 주제별 대화 이력 {total_count}건 ({topic_count}개 주제)",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# 대시보드 통계 블록
# ---------------------------------------------------------------------------

def build_dashboard_blocks(
    stats: dict,
    fallback_questions: Optional[list[str]] = None,
    top_topics: Optional[list[tuple[str, int]]] = None,
) -> dict:
    """
    챗봇 대시보드 통계 Block Kit 페이로드를 반환한다.
    stats: get_dashboard_stats() 반환값.
    fallback_questions: get_recent_fallbacks() 반환값 (선택).
    """
    period_days = stats.get("period_days", 7)
    period_label = f"{period_days}일" if period_days != 30 else "30일(1개월)"

    total = stats.get("total_responses", 0)
    fallback = stats.get("fallback_count", 0)
    pos = stats.get("positive_feedback", 0)
    neg = stats.get("negative_feedback", 0)
    avg_ms = stats.get("avg_response_ms")
    prompt_tokens = stats.get("total_prompt_tokens", 0)
    completion_tokens = stats.get("total_completion_tokens", 0)
    actionable_count = stats.get("actionable_count", 0)
    non_actionable_count = stats.get("non_actionable_count", 0)
    avg_rag = stats.get("avg_rag_similarity")
    web_search_count = stats.get("web_search_count", 0)
    web_search_rate = stats.get("web_search_rate", 0.0)
    active_users = stats.get("active_users", 0)
    feedback_rate = stats.get("feedback_response_rate", 0.0)

    total_bot = total + fallback
    success_rate = f"{total / total_bot * 100:.0f}%" if total_bot else "N/A"
    avg_ms_str = f"{avg_ms / 1000:.1f}초" if avg_ms else "측정 중"
    prompt_str = f"{prompt_tokens:,}" if prompt_tokens else "측정 중"
    completion_str = f"{completion_tokens:,}" if completion_tokens else "측정 중"

    total_fb = pos + neg
    pos_rate_str = f"{pos / total_fb * 100:.0f}%" if total_fb else "N/A"
    feedback_str = (
        f":thumbsup: {pos}  :thumbsdown: {neg}  (총 {total_fb}건, 긍정 {pos_rate_str})"
        if total_fb else "피드백 없음"
    )
    feedback_rate_str = f"{feedback_rate * 100:.0f}%" if feedback_rate else "N/A"

    # RAG 평균 유사도 표시 및 품질 해석
    if avg_rag is not None:
        rag_bar = "🟢" if avg_rag >= 0.7 else ("🟡" if avg_rag >= 0.5 else "🔴")
        rag_str = f"{rag_bar} {avg_rag:.2f}  {'(충분)' if avg_rag >= 0.7 else '(지식 부족 가능)'}"
    else:
        rag_str = "측정 중"

    web_rate_str = f"{web_search_rate * 100:.0f}%  ({web_search_count}건)" if total_bot else "N/A"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 QNA BOT 대시보드 (최근 {period_label})",
                "emoji": True,
            },
        },
        {"type": "divider"},
        # 응답 현황
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*총 응답 수*\n{total}건"},
                {"type": "mrkdwn", "text": f"*응답 성공률*\n{success_rate}"},
                {"type": "mrkdwn", "text": f"*Fallback (담당자 호출)*\n{fallback}건"},
                {"type": "mrkdwn", "text": f"*평균 응답 시간*\n{avg_ms_str}"},
            ],
        },
        {"type": "divider"},
        # 사용자 현황
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*활성 사용자 수*\n{active_users}명"},
                {"type": "mrkdwn", "text": f"*피드백 응답률*\n{feedback_rate_str}"},
            ],
        },
        {"type": "divider"},
        # 피드백
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*사용자 피드백*\n{feedback_str}",
            },
        },
        {"type": "divider"},
        # RAG 품질 + 웹 검색 의존율
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*RAG 평균 유사도*\n{rag_str}"},
                {"type": "mrkdwn", "text": f"*웹 검색 의존율*\n{web_rate_str}"},
            ],
        },
        {"type": "divider"},
        # 응답 필요 여부 분포
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*:question: 응답필요*\n{actionable_count}건"},
                {"type": "mrkdwn", "text": f"*:speech_balloon: 기타*\n{non_actionable_count}건"},
            ],
        },
        {"type": "divider"},
        # 토큰 사용량
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*입력 토큰 (요청, 추정)*\n{prompt_str}"},
                {"type": "mrkdwn", "text": f"*출력 토큰 (응답, 추정)*\n{completion_str}"},
            ],
        },
    ]

    # 자주 묻는 주제 섹션
    if top_topics:
        topic_lines = "\n".join(
            f"`{i}.` {t}  _{cnt}건_"
            for i, (t, cnt) in enumerate(top_topics, 1)
        )
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*:label: 자주 묻는 주제 Top {len(top_topics)}*\n{topic_lines}",
                },
            }
        )

    # Fallback 트리거 키워드 섹션
    if fallback_questions:
        fb_lines = "\n".join(
            f"`{i}.` {q[:60]}{'…' if len(q) > 60 else ''}"
            for i, q in enumerate(fallback_questions, 1)
        )
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*:warning: 최근 Fallback 트리거 질문*\n{fb_lines}",
                },
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        ":robot_face: 응답 시간·토큰·RAG 유사도는 이 기능 배포 이후 데이터부터 집계됩니다. "
                        "초기에는 일부 항목이 '측정 중'으로 표시될 수 있습니다."
                    ),
                }
            ],
        }
    )

    return {
        "text": f"QNA BOT 대시보드 (최근 {period_label}): 총 응답 {total}건",
        "blocks": blocks,
    }
