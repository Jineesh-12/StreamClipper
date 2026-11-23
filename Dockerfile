FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=7860
ENV STREAMCLIPS_OUT_DIR=/data/StreamClips
ENV STREAMCLIPS_HOME=/data/StreamClips

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    fonts-dejavu-core \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
      faster-whisper \
      opencv-python-headless \
      pillow \
      numpy \
      "yt-dlp[default]" \
      google-genai

RUN python -c "from pathlib import Path; from faster_whisper import WhisperModel; p = Path.home() / '.streamclips' / 'models' / 'whisper'; p.mkdir(parents=True, exist_ok=True); [WhisperModel(m, device='cpu', compute_type='int8', download_root=str(p)) for m in ('tiny', 'base')]"

RUN mkdir -p /data/StreamClips && chmod -R 777 /data

EXPOSE 7860
CMD ["python", "-u", "web_ui.py"]
