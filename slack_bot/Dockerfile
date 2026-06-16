# Slack 챗봇 Python 앱 컨테이너 빌드 설정 (fastembed + Supabase 기반)
FROM python:3.11-slim

# fastembed ONNX 런타임이 요구하는 libgomp (OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 복사 (캐시 레이어 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# fastembed 임베딩 모델 사전 다운로드 (빌드 시 캐싱 — 실행 시 지연 없음)
RUN python -c "\
from fastembed import TextEmbedding; \
list(TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-mpnet-base-v2').embed(['warmup']))"

# 앱 소스 복사
COPY . .

# 비루트 사용자로 실행
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]
