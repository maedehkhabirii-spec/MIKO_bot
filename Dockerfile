# syntax=docker/dockerfile:1

FROM python:3.11-slim

# تنظیم متغیرهای محیطی پایتون و pip
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# نصب ابزارهای ضروری و FFmpeg در یک لایه
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ساخت کاربر غیر Root
RUN useradd -m -u 10001 appuser

# آپگرید pip و نصب نیازمندی‌ها قبل از کپی کردن کدها (جهت استفاده حداکثری از Cache داکر)
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt && \
    python -m pip install bgutil-ytdlp-pot-provider

# ساخت پوشه دانلود و تنظیم دسترسی قبل از کپی کد
RUN mkdir -p /app/downloads && chown -R appuser:appuser /app

# کپی کردن کدها با مالکیت مستقیم appuser (بدون نیاز به chown مجدد و ایجاد لایه اضافه)
COPY --chown=appuser:appuser . .

# تغییر کاربر به appuser
USER appuser

# بررسی وضعیت سلامت سرویس (Healthcheck)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${PORT}/ || exit 1

# دستور اجرای ربات
CMD ["python", "bot.py"]
