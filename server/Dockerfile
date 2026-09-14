# Gateway container — imports service/pipeline.py in-process (default mode),
# so this image bundles BOTH server/ and service/ code + dependencies. Build
# context must be the repo root (see docker-compose.yml: `context: ..`).
#
# Platform-agnostic: same convention as service/Dockerfile — all host config
# via env vars, no code branches per platform.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=4 \
    PORT=8090

WORKDIR /app

# service/'s own requirements (VLM + OCR stack) first, then the gateway's.
COPY service/requirements.txt ./service-requirements.txt
RUN pip install --no-cache-dir \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
    -r service-requirements.txt

COPY server/requirements.txt ./server-requirements.txt
RUN pip install --no-cache-dir -r server-requirements.txt

# service/ code (pipeline import target) — models/ is NOT copied (3 GB;
# downloads at runtime, same as service/Dockerfile).
COPY service/config.py service/ocr.py service/pipeline.py /app/service/

# server/ code
COPY server/config.py server/cache.py server/ratelimit.py server/metrics.py \
     server/providers.py server/app.py server/analyze_metrics.py /app/server/

WORKDIR /app/server
ENV SERVICE_IMPORT_PATH=/app/service

VOLUME ["/data"]
EXPOSE 8090
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
