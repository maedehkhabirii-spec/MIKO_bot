```dockerfile
# syntax=docker/dockerfile:1

FROM python:3.11-slim

# نصب ابزارهای ضروری
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ایجاد user غیرroot برای امنیت
RUN useradd -m -u 10001 appuser

# بهتر است pip دقیق‌تر و قابل‌پیش‌بینی‌تر نصب شود
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# کپی requirements و نصب
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

# اگر واقعاً این پکیج لازم است، بهتر است داخل requirements.txt باشد.
# فعلاً (مطابق Dockerfile قبلی شما) همینجا هم نصب می‌کنیم تا رفتار تغییر نکند.
# پیشنهاد: نسخه را پین کنید (مثلاً bgutil-ytdlp-pot-provider==x.y.z)
RUN python -m pip install bgutil-ytdlp-pot-provider

# کپی کد اصلی
COPY . .

# فولدر دانلود
RUN mkdir -p downloads && chown -R appuser:appuser /app

USER appuser

CMD ["python", "bot.py"]
```
