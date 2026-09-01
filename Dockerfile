# syntax=docker/dockerfile:1

FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd -m -u 10001 appuser

COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

RUN mkdir -p /app/downloads && chown -R appuser:appuser /app

COPY --chown=appuser:appuser . .

# اعطای مجوز اجرا به اسکریپت ورود
RUN chmod +x /app/entrypoint.sh

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${PORT}/ || exit 1

# تنظیم اسکریپت ورود خودکار
ENTRYPOINT ["/app/entrypoint.sh"]

# دستور پیش‌فرض اجرا
CMD ["python", "bot.py"]
