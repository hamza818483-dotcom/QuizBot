FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-ben \
    fonts-noto \
    curl \
    gcc \
    python3-dev \
    chromium \
    fonts-noto-color-emoji \
    libraqm0 \
    libraqm-dev \
    libfribidi-dev \
    libharfbuzz-dev \
    && rm -rf /var/lib/apt/lists/*

ENV CHROMIUM_PATH=/usr/bin/chromium

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Force-rebuild Pillow from source against system libraqm so raqm (complex
# script shaping — needed for correct Bengali conjuncts) is actually linked
# in; prebuilt PyPI wheels usually ship WITHOUT raqm support.
RUN pip install --no-cache-dir --no-binary=:all: --force-reinstall pillow
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/data /app/logs

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]

# bust=1781027802
