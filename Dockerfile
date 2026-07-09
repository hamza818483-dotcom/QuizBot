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
    && rm -rf /var/lib/apt/lists/*

ENV CHROMIUM_PATH=/usr/bin/chromium

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/data /app/logs

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]

# bust=1781027802
