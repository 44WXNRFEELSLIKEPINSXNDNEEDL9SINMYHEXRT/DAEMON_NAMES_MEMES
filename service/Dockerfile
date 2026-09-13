# Daemon Names Memes classifier — platform-agnostic CPU image.
# Runs identically on HF Spaces (Docker SDK), Render, Fly.io, Railway, or any
# Docker-capable VPS. All host config via env vars (see .env.example); the
# image contains no platform-specific branches.
#
# Model weights (~3 GB) are NOT baked in — they download from the HF Hub on
# first boot into the HF cache. Attach a volume at /root/.cache/huggingface
# (or pre-seed /app/models) to make cold starts instant.
#
# Optional LightOnOCR engine: build with
#   docker build --build-arg INSTALL_LIGHTONOCR=1 -t daemon-names-memes .
FROM python:3.11-slim

# opencv (rapidocr dep) needs libGL + glib on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=4 \
    PORT=8080

WORKDIR /app

COPY requirements.txt requirements-lightonocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ARG INSTALL_LIGHTONOCR=0
RUN if [ "$INSTALL_LIGHTONOCR" = "1" ]; then \
        pip install --no-cache-dir -r requirements-lightonocr.txt; \
    fi

COPY config.py ocr.py pipeline.py app.py ./

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
