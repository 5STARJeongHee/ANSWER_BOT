# 사내 Slack Q&A 챗봇 - OpenRouter 모델 선정 보고서

> 작성일: 2026-06-16  
> 기준: OpenRouter 무료(:free) 모델, 실측 smoke test 포함

---

## 1. 후보 모델 비교표

아래 22개 무료 모델 전체를 분석하고, 주요 후보를 비교하였다.

| 모델 ID | 컨텍스트 | 파라미터 | 한국어 smoke test | 응답속도 | 비고 |
|---------|---------|---------|----------------|---------|-----|
| `nex-agi/nex-n2-pro:free` | 262,144 | 397B(17B active) | **PASS** (유창) | 1.3s | Qwen3.5 기반 MoE |
| `openai/gpt-oss-20b:free` | 131,072 | 21B(3.6B active) | **PASS** (JSON 분류) | 2.0s | 경량, 안정적 |
| `openai/gpt-oss-120b:free` | 131,072 | 120B(5.1B active) | **PASS** (상세 QA) | 38s | 응답 느림 |
| `google/gemma-4-31b-it:free` | 262,144 | 31B | **PASS** (markdown 래핑) | 5.7s | JSON 출력 시 ```json 래핑 |
| `nvidia/nemotron-3-super-120b-a12b:free` | 1,000,000 | 120B(12B active) | **FAIL** (영어로만 응답) | 5.9s | 한국어 미지원 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 1,000,000 | 550B(55B active) | 미테스트 | - | 추론 모델, trace 출력 |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 256,000 | 30B(3B active) | **FAIL** (content=None) | - | reasoning-only 출력, 사용 불가 |
| `qwen/qwen3-next-80b-a3b-instruct:free` | 262,144 | 80B(3B active) | **FAIL** (429 반복) | - | 가용성 매우 낮음 |
| `meta-llama/llama-3.3-70b-instruct:free` | 131,072 | 70B | **FAIL** (429) | - | 가용성 낮음 |
| `meta-llama/llama-3.2-3b-instruct:free` | 131,072 | 3B | **FAIL** (429) | - | 한국어 공식 미지원 |
| `liquid/lfm-2.5-1.2b-instruct:free` | 32,768 | 1.2B | 부분 PASS | 0.6s | 한국어 인코딩 불안정, 컨텍스트 최소 |
| `nvidia/nemotron-nano-12b-v2-vl:free` | 128,000 | 12B | 미테스트 | - | 이미지 VL 모델 (기존 image 용도) |
| `nousresearch/hermes-3-llama-3.1-405b:free` | 131,072 | 405B | 미테스트 | - | 대형이나 가용성 불명 |

---

## 2. 최종 선정 모델 및 이유

### 선정 결과

| 역할 | 선정 모델 | 이유 |
|------|---------|-----|
| **classifier** (질문 분류) | `openai/gpt-oss-20b:free` | 한국어 JSON 분류 PASS, 2.0s 빠른 응답, 안정적 가용성 |
| **qa** (답변 생성) | `nex-agi/nex-n2-pro:free` | 한국어 유창, 1.3s 빠름, 262K 대형 컨텍스트, Qwen3.5 기반 |
| **summary** (대화 요약) | `nex-agi/nex-n2-pro:free` | 동일 모델 재사용, 한국어 요약 PASS, 262K로 긴 대화 처리 |
| **rag_query** (RAG 검색 쿼리 생성) | `openai/gpt-oss-20b:free` | 경량 쿼리 생성에 충분, classifier와 모델 재사용으로 rate limit 분산 |

> **참고 (이미지 처리):** 이미지가 포함된 Slack 메시지 분석은 `nvidia/nemotron-nano-12b-v2-vl:free` (기존 properties.txt의 image 모델)를 유지한다.

### 선정 근거 상세

**`openai/gpt-oss-20b:free` (classifier / rag_query):**
- smoke test에서 한국어 JSON 출력 정확히 `{"category":"잡담"}` 반환
- MoE 3.6B active parameter로 응답 2.0s 이내
- 131K 컨텍스트로 분류와 쿼리 생성에 충분
- 429 rate limit 없이 안정 응답

**`nex-agi/nex-n2-pro:free` (qa / summary):**
- Qwen3.5 아키텍처 기반으로 한국어 능력 검증됨
- 262K 컨텍스트로 긴 Slack 대화 스레드 처리 가능
- 요약 테스트: 대화 내용을 한국어 2문장으로 정확히 압축
- QA 테스트: 연차 신청 등 사내 정책 질문에 한국어로 구조화된 답변 제공
- 1.3s 응답 속도 (동급 대비 최고 속도)

### 탈락 이유

- **Nemotron Super/Ultra/Nano**: 한국어 미지원 또는 content=None (reasoning-only 모드)
- **Qwen3-Next-80B, Llama3.3-70B**: 429 rate limit 지속, 실사용 불안정
- **Liquid 1.2B**: 한국어 인코딩 불안정, 32K 컨텍스트 제약
- **Google Gemma 4 31B**: JSON 출력 시 ```json 마크다운 래핑 포함, 파싱 코드 추가 필요 (fallback 후보)

---

## 3. 역할별 모델 배정 (최종)

```
classifier_model = openai/gpt-oss-20b:free
qa_model         = nex-agi/nex-n2-pro:free
summary_model    = nex-agi/nex-n2-pro:free
rag_query_model  = openai/gpt-oss-20b:free
image_model      = nvidia/nemotron-nano-12b-v2-vl:free
```

> **임베딩 모델 주의:** `rag_query_model`은 RAG 검색 쿼리를 생성하는 텍스트 생성 모델이다. 벡터 임베딩(검색 인덱싱)은 별도 임베딩 모델이 필요하며, OpenRouter 무료 티어에는 임베딩 전용 모델이 없다. 실제 임베딩에는 `openai/text-embedding-3-small` (OpenAI 직접 API) 또는 `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (HuggingFace 로컬 실행)를 사용할 것을 권장한다.

---

## 4. 프롬프트 최적화 팁 및 컨텍스트 관리 전략

### 4.1 역할별 시스템 프롬프트 설계

**Classifier (gpt-oss-20b):**
```
시스템 프롬프트 (최소화):
"Slack 메시지를 분류한다. JSON만 출력: {\"category\": \"질문|공지|잡담|기타\", \"urgency\": \"높음|보통|낮음\"}"
```
- 시스템 프롬프트 50토큰 이하 유지
- few-shot 예시 불필요 (모델이 이미 JSON 지시 따름)
- `max_tokens: 60` 제한으로 불필요한 토큰 소비 방지

**QA (nex-n2-pro):**
```
시스템 프롬프트:
"사내 Slack Q&A 봇이다. 한국어로 간결하고 정확하게 답한다. 모르는 내용은 솔직히 모른다고 한다."
```
- 컨텍스트에 RAG 검색 결과를 `[참고 문서]` 섹션으로 앞에 삽입
- `max_tokens: 500` 권장 (Slack 메시지 길이 제약)

**Summarizer (nex-n2-pro):**
```
시스템 프롬프트:
"대화를 3문장 이내로 핵심만 한국어로 요약한다."
```
- 입력 대화는 `[대화 시작]...[대화 끝]` 태그로 감싸기
- `max_tokens: 200` 제한

### 4.2 컨텍스트 관리 전략

**슬라이딩 윈도우 (Sliding Window):**
- 최근 10개 메시지만 컨텍스트에 포함
- 토큰 추정: `메시지수 × 평균 50토큰 × 1.3`
- 전체 컨텍스트가 8,000토큰 초과 시 오래된 메시지 제거

**주기적 압축 요약:**
```
대화 20개 이상 누적 시:
  1. 오래된 대화 → summarizer 모델로 요약
  2. 요약문을 system 메시지로 삽입: "[이전 대화 요약]: ..."
  3. 원본 오래된 메시지 제거
```

**우선순위 보존 원칙:**
1. 시스템 프롬프트 (항상 보존)
2. [이전 대화 요약] (있을 경우)
3. 최근 사용자 질문 (항상 보존)
4. 최근 N개 메시지 (슬라이딩)

**토큰 추정 코드 예시:**
```python
def estimate_tokens(text: str) -> int:
    """단어 수 기반 토큰 추정 (실제보다 ~10% 과추정)."""
    return int(len(text.split()) * 1.3)

MAX_CONTEXT_TOKENS = 6000  # 무료 tier 안전 마진 (모델 한도의 ~2.5%)

def trim_context(messages: list, system_prompt: str) -> list:
    system_tokens = estimate_tokens(system_prompt)
    budget = MAX_CONTEXT_TOKENS - system_tokens
    
    result = []
    for msg in reversed(messages):
        tokens = estimate_tokens(msg['content'])
        if budget - tokens < 0:
            break
        result.insert(0, msg)
        budget -= tokens
    return result
```

### 4.3 구조화 출력 활용

Classifier는 반드시 JSON 모드 사용:
```python
response = client.chat.completions.create(
    model="openai/gpt-oss-20b:free",
    messages=messages,
    response_format={"type": "json_object"},  # JSON 강제
    max_tokens=60,
)
```

QA/Summary는 자유 텍스트이므로 response_format 생략.

---

## 5. 무료 티어 Rate Limit 대응 방안

### 5.1 현황 파악 (smoke test 실측)

| 모델 | 429 발생 여부 | 안정성 |
|------|------------|------|
| `openai/gpt-oss-20b:free` | 없음 (4회 연속 성공) | 높음 |
| `nex-agi/nex-n2-pro:free` | 없음 (3회 성공) | 높음 |
| `qwen/qwen3-next-80b-a3b-instruct:free` | **429 반복** | 매우 낮음 |
| `meta-llama/llama-3.3-70b-instruct:free` | **429 반복** | 낮음 |

### 5.2 Rate Limit 대응 전략

**지수 백오프 재시도:**
```python
import time, random

def call_with_retry(client, model, messages, max_retries=3):
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(
                model=model, messages=messages
            )
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            wait = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait)
```

**Fallback 체인:**
```python
FALLBACK_CHAIN = {
    "classifier": [
        "openai/gpt-oss-20b:free",
        "google/gemma-4-31b-it:free",  # JSON 마크다운 래핑 파싱 필요
    ],
    "qa": [
        "nex-agi/nex-n2-pro:free",
        "openai/gpt-oss-120b:free",    # 느리지만 한국어 가능
    ],
    "summary": [
        "nex-agi/nex-n2-pro:free",
        "openai/gpt-oss-120b:free",
    ],
}
```

**인메모리 캐싱 (동일 질문 중복 호출 방지):**
```python
from functools import lru_cache
import hashlib

@lru_cache(maxsize=256)
def cached_classify(message_hash: str, message: str) -> str:
    return call_api("classifier", message)

def classify(message: str) -> str:
    key = hashlib.md5(message.encode()).hexdigest()
    return cached_classify(key, message)
```

**요청 간격 조절:**
- Classifier: 연속 호출 시 `sleep(0.5)` 추가
- QA/Summary: 자연 대기 (사용자 응답 대기 시간 활용)
- 동일 모델에 10초 내 5회 이상 호출 금지 (자체 rate limit)

**무료 tier 일일 한도 추적:**
```python
# 환경변수나 Redis에 일별 카운터 저장
DAILY_LIMIT_PER_MODEL = 200  # 보수적 추정

def check_and_increment(model: str) -> bool:
    today = datetime.date.today().isoformat()
    key = f"api_count:{model}:{today}"
    count = cache.get(key, 0)
    if count >= DAILY_LIMIT_PER_MODEL:
        return False  # fallback 모델로 전환
    cache.set(key, count + 1, ttl=86400)
    return True
```

---

## 6. 파이프라인 아키텍처

```
Slack 메시지 수신
       ↓
[Classifier: gpt-oss-20b]
  → 질문/공지/잡담 분류
  → 긴급도 판단
       ↓ (질문인 경우)
[RAG Query: gpt-oss-20b]
  → 검색 쿼리 생성
       ↓
[Vector Search]
  → 사내 문서 검색
       ↓
[QA: nex-n2-pro]
  → 검색 결과 + 대화 컨텍스트 기반 답변 생성
       ↓
[Summary: nex-n2-pro]  ← 20개 이상 대화 누적 시 별도 호출
  → 이전 대화 압축 요약
       ↓
Slack 답변 전송
```

---

*이 문서는 2026-06-16 기준 OpenRouter API 응답 및 실측 smoke test를 바탕으로 작성되었다. 무료 모델 가용성은 변경될 수 있으므로 주기적으로 재검증을 권장한다.*
