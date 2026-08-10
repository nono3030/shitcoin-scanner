# Fly.io / container image for shitcoin-scanner (dashboard + daily job)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/data \
    BIND_HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# System CA certs for HTTPS (Bybit / Kraken)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || true

COPY . .

# Persistent dirs (Fly volume mounts /data)
RUN mkdir -p /data/live /data/cache /data/out /data/paper \
    && chmod +x scripts/fly_*.sh 2>/dev/null || true

EXPOSE 8080

# Default: web dashboard (override for cron with run_daily)
CMD ["python", "dashboard.py", "--serve", "--no-open", "--host", "0.0.0.0", "--port", "8080"]
