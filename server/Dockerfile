# Gateway image — cache + metrics only, no model stack (see requirements.txt).
# Build context is server/ itself: `docker build -t dnm-gateway server/`.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8090 \
    WEB_CONCURRENCY=2 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    METRICS_DB_PATH=/data/metrics.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py cache.py metrics.py providers.py app.py analyze_metrics.py ./

RUN useradd --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin gateway \
    && mkdir -p /data && chown gateway:gateway /data
USER gateway

VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

# uvicorn reads WEB_CONCURRENCY and FORWARDED_ALLOW_IPS from the environment.
CMD ["sh", "-c", "exec uvicorn app:app --host \"$HOST\" --port \"$PORT\" --proxy-headers --no-server-header"]
