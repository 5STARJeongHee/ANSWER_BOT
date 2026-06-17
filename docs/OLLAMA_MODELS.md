# Ollama 로컬 LLM 모델 운용 가이드

서버 사양: Ubuntu 20.04 / Xeon Silver 4208 x2 / 251GB RAM / GPU 없음 (CPU 추론)

---

## 1. 백엔드 전환 방법

`.env` 파일에서 `LLM_BACKEND` 값만 바꾸면 됩니다. 코드 변경 없이 앱 재시작만 필요합니다.

```bash
# OpenRouter 사용 (기본)
LLM_BACKEND=openrouter

# 로컬 Ollama 사용
LLM_BACKEND=ollama
LLM_TIMEOUT=120.0
```

---

## 2. properties.txt 설정 구조

```
# 각 역할별 Ollama 모델명 (ollama list 에서 확인되는 이름)
ollama_qa_model:          <QA 답변 모델>
ollama_summary_model:     <요약 모델>
ollama_classifier_model:  <질문 분류 / RAG 쿼리 생성>
ollama_rag_query_model:   <RAG 검색어 생성 (classifier와 같아도 됨)>
ollama_image:             <이미지 + 텍스트 멀티모달 모델>
```

> **변경 후 앱만 재시작** (`docker compose restart app`) 으로 즉시 반영됩니다.
> Ollama 서버 재시작은 불필요합니다.

---

## 3. 모델 메모리 계산

```
Q4_K_M  약 파라미터(B) x 0.54 GB
Q8_0    약 파라미터(B) x 1.0  GB
```

| 파라미터 | Q4_K_M RAM |
|---------|-----------|
| 2B      | ~1.1 GB   |
| 4B      | ~2.2 GB   |
| 7B      | ~3.8 GB   |
| 8B      | ~4.3 GB   |
| 12B     | ~6.5 GB   |
| 14B     | ~7.6 GB   |
| 32B     | ~17 GB    |
| 70B     | ~38 GB    |

서버 여유 메모리 약 200GB 기준으로 여러 모델을 동시에 올릴 수 있습니다.

---

## 4. 역할별 추천 모델 목록

### 4-1. QA / 요약 (ollama_qa_model, ollama_summary_model)

한국어 이해 + 긴 답변 생성이 핵심입니다.

| 모델 | 크기 | RAM | 한국어 | 특징 |
|------|------|-----|--------|------|
| `qwen2.5:32b-instruct-q4_K_M` | 32B | ~17GB | 최상 | 오픈소스 한국어 최고 성능 |
| `qwen2.5:14b-instruct-q4_K_M` | 14B | ~7.6GB | 우수 | 속도·성능 균형 |
| `qwen2.5:7b-instruct-q4_K_M` | 7B | ~3.8GB | 양호 | 가장 빠름, 간단한 QA용 |
| `hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M` | 12B | ~6.5GB | 양호 | 코딩/에러 로그 특화 |
| `llama3.1:70b-instruct-q4_K_M` | 70B | ~38GB | 우수 | 최고 성능, 가장 느림 |

HuggingFace 링크:
- Qwen2.5 계열: https://hf.co/Qwen/Qwen2.5-32B-Instruct
- Gemma-4-12B 코딩: https://hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF
- Llama 3.1 70B: https://hf.co/meta-llama/Llama-3.1-70B-Instruct

### 4-2. 분류기 / RAG 쿼리 (ollama_classifier_model, ollama_rag_query_model)

짧은 분류 작업이므로 작고 빠른 모델이 적합합니다.

| 모델 | 크기 | RAM | 한국어 | 특징 |
|------|------|-----|--------|------|
| `exaone3.5:2.4b-it-q4_K_M` | 2.4B | ~1.3GB | 최상 | LG AI Research, 한국어 소형 최강 |
| `exaone3.5:7.8b-it-q4_K_M` | 7.8B | ~4.2GB | 최상 | EXAONE 상위 버전 |
| `qwen2.5:7b-instruct-q4_K_M` | 7B | ~3.8GB | 우수 | 안정적, 보편적 |

HuggingFace 링크:
- EXAONE 3.5 2.4B: https://hf.co/LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct
- EXAONE 3.5 7.8B: https://hf.co/LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct

### 4-3. 이미지 분석 (ollama_image)

Slack에서 받은 이미지(에러 스크린샷 등)를 분석합니다.

| 모델 | 크기 | RAM | 한국어 | 특징 |
|------|------|-----|--------|------|
| `qwen2.5vl:7b-q4_K_M` | 7B | ~5GB | 최상 | 한국어+이미지 현재 최고 |
| `minicpm-v:8b-q4_K_M` | 8B | ~4.3GB | 우수 | 문서 이미지 인식 강점 |
| `llava:13b-v1.6-mistral-q4_K_M` | 13B | ~7GB | 보통 | 안정적, 영어 위주 |

HuggingFace 링크:
- Qwen2-VL 7B: https://hf.co/Qwen/Qwen2-VL-7B-Instruct
- MiniCPM-V: https://hf.co/openbmb/MiniCPM-V-2_6

---

## 5. 테스트 조합 시나리오

### 조합 A — 경량 / 에러 로그 특화 (RAM ~13GB)

에러 로그 분석이 주요 목적. 응답 속도 우선.

```properties
ollama_qa_model: hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M
ollama_summary_model: hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M
ollama_classifier_model: exaone3.5:2.4b-it-q4_K_M
ollama_rag_query_model: exaone3.5:2.4b-it-q4_K_M
ollama_image: qwen2.5vl:7b-q4_K_M
```

### 조합 B — 한국어 균형 (RAM ~27GB)

일반 업무 QA + 에러 분석 균형. 현재 기본값.

```properties
ollama_qa_model: qwen2.5:32b-instruct-q4_K_M
ollama_summary_model: qwen2.5:32b-instruct-q4_K_M
ollama_classifier_model: exaone3.5:2.4b-it-q4_K_M
ollama_rag_query_model: exaone3.5:2.4b-it-q4_K_M
ollama_image: qwen2.5vl:7b-q4_K_M
```

### 조합 C — 코딩 특화 + 한국어 분류 (RAM ~22GB)

에러 로그가 많고 한국어 분류 정확도도 필요한 경우.

```properties
ollama_qa_model: hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M
ollama_summary_model: qwen2.5:14b-instruct-q4_K_M
ollama_classifier_model: exaone3.5:2.4b-it-q4_K_M
ollama_rag_query_model: exaone3.5:2.4b-it-q4_K_M
ollama_image: qwen2.5vl:7b-q4_K_M
```

### 조합 D — 한국어 최대 성능 (RAM ~45GB)

한국어 답변 품질 최우선. 속도는 느림.

```properties
ollama_qa_model: qwen2.5:32b-instruct-q4_K_M
ollama_summary_model: qwen2.5:32b-instruct-q4_K_M
ollama_classifier_model: exaone3.5:7.8b-it-q4_K_M
ollama_rag_query_model: exaone3.5:7.8b-it-q4_K_M
ollama_image: qwen2.5vl:7b-q4_K_M
```

---

## 6. 모델 Pull 명령어

### Ollama Hub 공식 모델

```bash
# QA / 요약
docker exec slackbot_ollama ollama pull qwen2.5:32b-instruct-q4_K_M
docker exec slackbot_ollama ollama pull qwen2.5:14b-instruct-q4_K_M
docker exec slackbot_ollama ollama pull qwen2.5:7b-instruct-q4_K_M
docker exec slackbot_ollama ollama pull llama3.1:70b-instruct-q4_K_M

# 분류기 / RAG
docker exec slackbot_ollama ollama pull exaone3.5:2.4b-it-q4_K_M
docker exec slackbot_ollama ollama pull exaone3.5:7.8b-it-q4_K_M

# 이미지
docker exec slackbot_ollama ollama pull qwen2.5vl:3b-q4_K_M
docker exec slackbot_ollama ollama pull qwen2.5vl:7b-q4_K_M
docker exec slackbot_ollama ollama pull llava:13b-v1.6-mistral-q4_K_M
docker exec slackbot_ollama ollama pull minicpm-v:8b-q4_K_M
```

### HuggingFace 직접 Pull (Ollama 0.6.4 이상)

```bash
docker exec slackbot_ollama ollama pull hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q4_K_M
```

### 설치된 모델 목록 확인

```bash
docker exec slackbot_ollama ollama list
```

---

## 7. 모델 교체 전체 절차

```bash
# 1. 모델 사전 다운로드 (앱 운영 중에도 가능)
docker exec slackbot_ollama ollama pull <모델명>

# 2. properties.txt 수정 (원하는 조합으로 변경)
vi properties.txt

# 3. 앱만 재시작 (Ollama는 그대로 유지)
docker compose restart app

# 4. 로그 확인
docker compose logs -f app
```

---

## 8. 라이선스 요약

| 모델 계열 | 라이선스 | 사내 사용 | 상업 서비스 |
|----------|---------|---------|------------|
| Qwen2.5 | Apache 2.0 | 가능 | 가능 |
| EXAONE 3.5 | LGPL-3.0 | 가능 | 가능 (조건부) |
| Llama 3.1 | Llama 커뮤니티 | 가능 | MAU 7억 미만 허용 |
| Gemma 4 | Gemma 라이선스 | 가능 | 세부 조건 확인 필요 |
