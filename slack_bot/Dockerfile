# Slack 챗봇 Python 앱 컨테이너 빌드 설정
FROM python:3.11-slim

WORKDIR /app

# 시스템 의존성 설치 (psycopg2-binary, 임베딩 모델용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 (캐시 레이어 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 임베딩 모델 사전 다운로드 (빌드 시 캐싱)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')"

# 앱 소스 복사
COPY . .

# 비루트 사용자로 실행
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]
