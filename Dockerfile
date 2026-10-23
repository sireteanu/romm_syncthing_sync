# romm_sync — based on romm_syncthing_sync by amn-96 (MIT). GUI + two-way sync fork.
FROM python:3.13-slim

LABEL org.opencontainers.image.title="romm_sync" \
      org.opencontainers.image.description="Sync emulator saves/states between a Syncthing folder and RomM, with a web dashboard and two-way sync" \
      org.opencontainers.image.source="https://github.com/sireteanu/romm_syncthing_sync" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (includes src/web_static for the GUI)
COPY src/ ./src/
RUN rm -rf ./src/experimental
COPY romm_sync.py VERSION ./

# Persistent data (sync history, pending approvals, backups). Map to appdata on Unraid.
ENV DATA_DIR=/data \
    WEB_PORT=8080 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data /syncdata /config
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('WEB_PORT','8080'), timeout=5)" || exit 1

CMD ["python", "romm_sync.py"]
