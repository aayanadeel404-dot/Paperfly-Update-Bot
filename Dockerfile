# syntax=docker/dockerfile:1
FROM python:3.11-slim

# System deps for Playwright/Firefox
RUN apt-get update && apt-get install -y \
    wget curl ca-certificates \
    libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libpangocairo-1.0-0 libgtk-3-0 libdbus-glib-1-2 \
    libx11-xcb1 libxcb-dri3-0 fonts-liberation \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Firefox browser
RUN python -m playwright install firefox
RUN python -m playwright install-deps firefox

COPY bot.py .

CMD ["python", "bot.py"]
