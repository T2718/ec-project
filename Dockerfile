FROM python:3.10-slim

# 各種依存パッケージ、ffmpeg、Google Chromeのインストール
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    curl \
    unzip \
    ffmpeg \
    && mkdir -p /etc/apt/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

# 依存ライブラリのインストール
RUN pip install --no-cache-dir -r requirements.txt uvicorn gunicorn

# Renderから割り当てられる PORT 環境変数（無ければ5000）で起動
CMD exec gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-5000} --timeout 300 main:app
