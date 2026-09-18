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
# 2026-09-13: Deno kept installed as a fallback JS runtime. Primary path
# is yt-dlp's --remote-components ejs:github (no local runtime needed),
# but that requires the container to reach GitHub at request time -- on
# an unstable network tier that can itself fail. Deno being present lets
# the app retry with --js-runtimes deno if the remote-component path
# fails, instead of having only one way to solve the nsig challenge.
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno
ENV PATH="/root/.deno/bin:${PATH}"

# 2026-09-13: Cloudflare WARP was tried as a free proxy workaround for HF
# free-tier's unstable outbound connection to YouTube's CDN, and removed
# after live testing confirmed it: HF Space containers don't grant the
# NET_ADMIN capability WARP's tunnel needs, so it can never come up here
# (not a "might work" -- /warpstatus confirmed it stayed down). Use an
# external proxy service via the YT_PROXY env var instead (see app.py) --
# that only needs a plain outbound socket connection, no kernel tunnel.
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

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]

# bust=1781027802
