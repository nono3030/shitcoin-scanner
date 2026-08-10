# Fly.io image: dashboard + daily post-close scheduler (single machine / volume)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/data \
    BIND_HOST=0.0.0.0 \
    PORT=8080 \
    DAILY_RUN_UTC_HOUR=0 \
    DAILY_RUN_UTC_MINUTE=15 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || true

COPY . .

RUN mkdir -p /data/live /data/cache /data/out /data/paper \
    && chmod +x scripts/*.sh 2>/dev/null || true

EXPOSE 8080

# Web (thread) + daily scheduler at 00:15 UTC (post daily candle close)
CMD ["python", "scripts/fly_entrypoint.py"]
