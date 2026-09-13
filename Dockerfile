FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-ben \
    fonts-noto \
    curl \
    unzip \
    gcc \
    python3-dev \
    chromium \
    ffmpeg \
    fonts-noto-color-emoji \
    libraqm0 \
    libraqm-dev \
    libfribidi-dev \
    libharfbuzz-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libfreetype-dev \
    libopenjp2-7-dev \
    libtiff5-dev \
    && rm -rf /var/lib/apt/lists/*

ENV CHROMIUM_PATH=/usr/bin/chromium

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 2026-09-13: force latest yt-dlp at build time -- YouTube changes its
# player/cipher frequently and an outdated yt-dlp breaks silently
# (extraction failures, wrongly-blamed as network/SSL errors). requirements.txt
# pins a version at write-time; this always overrides it with whatever is
# newest when the image is built.
RUN pip install --no-cache-dir -U yt-dlp yt-dlp-ejs
# 2026-09-13 ROOT CAUSE FIX for the observed 'SSL: UNEXPECTED_EOF_
# WHILE_READING' errors on /cut <yt-link>: as of 2026, YouTube extraction
# requires a JS runtime (yt-dlp's "EJS" system) to solve the nsig/
# signature challenge. Without one, yt-dlp still LOOKS like it works
# (extracts title/formats fine) but signs the actual media URL wrong --
# YouTube then aborts that connection mid-download, which surfaces as an
# SSL/EOF error even though the real cause is a missing JS runtime, not
# network flakiness. Deno is yt-dlp's officially recommended runtime.
# Docs: https://github.com/yt-dlp/yt-dlp/wiki/EJS
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno
ENV PATH="/root/.deno/bin:${PATH}"

# 2026-09-13 (user request): free workaround for HF Space FREE TIER's
# unstable outbound connection to YouTube's CDN (confirmed root cause of
# the persistent 'SSL: UNEXPECTED_EOF_WHILE_READING' errors -- fails even
# on the very first webpage/API fetch, every retry, on this hosting tier).
# Cloudflare WARP is a genuinely free VPN (no account/payment needed --
# 'warp-cli registration new' self-registers) whose IPs YouTube does NOT
# blacklist the way it does most datacenter proxies. Installed here as a
# local SOCKS5 proxy (127.0.0.1:40000); the app sets YT_PROXY to point at
# it automatically IF the daemon starts successfully at runtime (see
# entrypoint script) -- if WARP can't run in this container (e.g. no
# NET_ADMIN capability), /cut simply falls back to a direct connection,
# exactly as before this change, so it can't make things worse.
RUN apt-get update && apt-get install -y gnupg \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update && apt-get install -y cloudflare-warp \
    && rm -rf /var/lib/apt/lists/* \
    || echo "[build] WARP install failed/unavailable -- /cut will use a direct connection instead"
COPY scripts/start_warp.sh /app/scripts/start_warp.sh
RUN chmod +x /app/scripts/start_warp.sh || true
# Rebuild Pillow from source against system libraqm so raqm (complex script
# shaping — needed for correct Bengali conjuncts) is actually linked in;
# prebuilt PyPI wheels ship without raqm. If this ever fails to build, the
# app still runs — /slide falls back to unshaped rendering (with a log
# warning) instead of the whole image failing.
RUN pip install --no-cache-dir --no-binary=:all: --force-reinstall pillow \
    || pip install --no-cache-dir --force-reinstall pillow
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/data /app/logs

CMD ["/bin/sh", "-c", "/app/scripts/start_warp.sh & uvicorn app:app --host 0.0.0.0 --port 7860"]

# bust=1781027802
