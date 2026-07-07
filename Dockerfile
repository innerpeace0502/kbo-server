FROM python:3.11-slim

# Chromium 설치
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV PORT=8080
# --threads 4: sync 워커 1개는 느린 요청(스크레이핑 트리거) 하나가 모든 요청을 막아
# /api/scores가 30초+ 매달리던 head-of-line blocking 발생 → gthread로 동시 처리
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "120"]