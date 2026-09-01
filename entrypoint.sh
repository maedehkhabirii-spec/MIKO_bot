#!/bin/sh
set -e

echo "🔄 Checking for yt-dlp updates..."
pip install --no-cache-dir --upgrade yt-dlp bgutil-ytdlp-pot-provider

echo "🚀 Starting MIKO Telegram Bot..."
exec "$@"
