FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    STREAMCLIPS_NO_ENV_FALLBACK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /root/.streamclips/venv && \
    /root/.streamclips/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /root/.streamclips/venv/bin/pip install --no-cache-dir --upgrade \
      faster-whisper \
      opencv-python-headless \
      pillow \
      numpy \
      "yt-dlp[default]" \
      google-genai

RUN mkdir -p /root/.streamclips/models/whisper && \
    /root/.streamclips/venv/bin/python -c "from faster_whisper import WhisperModel; [WhisperModel(m, device='cpu', compute_type='int8', download_root='/root/.streamclips/models/whisper') for m in ('tiny', 'base')]"

COPY . /app

RUN mkdir -p /tmp/StreamClips

EXPOSE 8765

CMD ["python", "-u", "web_ui.py"]