FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV RECORDINGS_DIR=/recordings
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Virtual display
    xvfb \
    x11-xserver-utils \
    # Virtual audio
    pulseaudio \
    pulseaudio-utils \
    # Recording
    ffmpeg \
    # Python
    python3 \
    python3-pip \
    # Playwright system deps
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libglib2.0-0 \
    libnspr4 \
    # Secret-link remote desktop for Zoom login (bot/login_link.py)
    x11vnc \
    novnc \
    # Misc
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Temporary public tunnel for the login link; same pinned build as
# scripts/setup-login-link.sh (only published checksum is for amd64).
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        curl -fsSL --retry 2 --max-time 120 -o /usr/local/bin/cloudflared \
            https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64 \
        && echo "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872  /usr/local/bin/cloudflared" | sha256sum -c - \
        && chmod +x /usr/local/bin/cloudflared; \
    else \
        echo "cloudflared not installed for $(dpkg --print-architecture); set ZOOM_LOGIN_MODE=telegram to log in through chat"; \
    fi

WORKDIR /app

COPY requirements.txt .
# Ubuntu 22.04 ships pip 22.0.2, which crashes (AssertionError in the resolver)
# on some dependency graphs; upgrade it before resolving.
RUN pip3 install --no-cache-dir -U pip \
    && pip3 install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

ARG WHISPER_MODEL=medium
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY . .
RUN chmod +x entrypoint.sh

VOLUME ["/recordings"]

ENTRYPOINT ["./entrypoint.sh"]
