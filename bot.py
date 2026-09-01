import os
import asyncio
import time
import logging
import re
import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from dotenv import load_dotenv

import aiohttp
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile,
    CallbackQuery, Message
)
from aiogram.enums import ParseMode, ChatAction
import yt_dlp
from aiohttp import web

# ============================================================================
# لاگینگ و دریافت توکن
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("MIKO_BOT")

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.critical("❌ متغیر BOT_TOKEN یافت نشد! لطفا در تنظیمات Render آن را ست کنید.")
    # جهت جلوگیری از کرش ناگهانی کانتینر در رندر
    time.sleep(10)
    exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ============================================================================
# تنظیمات پروژه‌
# ============================================================================
@dataclass
class Config:
    RATE_LIMIT: int = 15
    TIME_WINDOW: int = 3600
    MAX_FILE_SIZE: int = 48 * 1024 * 1024
    CHUNK_SIZE: int = 40 * 1024 * 1024
    DOWNLOAD_TIMEOUT: int = 300
    CACHE_TTL: int = 3600
    AUTO_UPDATE_INTERVAL: int = 86400
    MAX_PLAYLIST_ITEMS: int = 10
    DOWNLOAD_DIR: str = "downloads"
    COOKIES_FILE: str = "cookies.txt"

config = Config()

class URLStore:
    def __init__(self):
        self._map: Dict[str, str] = {}
    def save(self, url: str) -> str:
        key = hashlib.md5(url.encode()).hexdigest()[:10]
        self._map[key] = url
        return key
    def get(self, key: str) -> Optional[str]:
        return self._map.get(key)

url_store = URLStore()

class SmartCache:
    def __init__(self, ttl: int = 3600):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._ttl = ttl
    def get(self, url: str, mode: str) -> Optional[Dict]:
        key = hashlib.md5(f"{url}:{mode}".encode()).hexdigest()
        if key in self._cache:
            data = self._cache[key]
            if time.time() - data['timestamp'] < self._ttl:
                return data['info']
        return None
    def set(self, url: str, mode: str, info: Dict):
        key = hashlib.md5(f"{url}:{mode}".encode()).hexdigest()
        self._cache[key] = {'info': info, 'timestamp': time.time()}

cache = SmartCache()

class RateLimiter:
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self._user_requests: Dict[int, List[float]] = {}
    def check(self, user_id: int) -> tuple[bool, int]:
        current_time = time.time()
        if user_id not in self._user_requests:
            self._user_requests[user_id] = []
        self._user_requests[user_id] = [t for t in self._user_requests[user_id] if current_time - t < self.time_window]
        if len(self._user_requests[user_id]) >= self.max_requests:
            return False, int(self.time_window - (current_time - self._user_requests[user_id][0]))
        self._user_requests[user_id].append(current_time)
        return True, 0
    def get_usage(self, user_id: int) -> tuple[int, int]:
        current_time = time.time()
        active = [t for t in self._user_requests.get(user_id, []) if current_time - t < self.time_window]
        return len(active), self.max_requests

rate_limiter = RateLimiter(config.RATE_LIMIT, config.TIME_WINDOW)

# ============================================================================
# توابع کمکی و تنظیمات yt-dlp
# ============================================================================
def format_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0: return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def format_duration(seconds: int) -> str:
    if not seconds: return "نامشخص"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def detect_platform(url: str) -> str:
    u = url.lower()
    if 'youtube.com' in u or 'youtu.be' in u: return 'youtube'
    if 'instagram.com' in u: return 'instagram'
    if 'spotify.com' in u: return 'spotify'
    return 'other'

def get_youtube_opts() -> Dict:
    return {
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['webpage', 'configs'],
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
    }

def get_base_opts(download_dir: str) -> Dict:
    opts = {
        'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
    }
    if os.path.exists(config.COOKIES_FILE):
        opts['cookiefile'] = config.COOKIES_FILE
    return opts

# ============================================================================
# کیبوردها
# ============================================================================
def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 راهنما", callback_data="ui_help"), InlineKeyboardButton(text="📊 وضعیت", callback_data="ui_stats")],
        [InlineKeyboardButton(text="🌐 پلتفرم‌ها", callback_data="ui_platforms")]
    ])

def build_media_keyboard(url_key: str, is_yt: bool = True) -> InlineKeyboardMarkup:
    if is_yt:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 1080p", callback_data=f"dl:1080:{url_key}"), InlineKeyboardButton(text="🎬 720p", callback_data=f"dl:720:{url_key}")],
            [InlineKeyboardButton(text="⚡ Best", callback_data=f"dl:best:{url_key}"), InlineKeyboardButton(text="🎵 MP3", callback_data=f"dl:audio:{url_key}")],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 دانلود", callback_data=f"dl:best:{url_key}"), InlineKeyboardButton(text="🎵 صوتی", callback_data=f"dl:audio:{url_key}")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")]
    ])

# ============================================================================
# هندلرها
# ============================================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    usage, max_u = rate_limiter.get_usage(message.from_user.id)
    await message.answer(
        f"✨ <b>سلام {message.from_user.first_name}! به MIKO Downloader خوش آمدید.</b>\n\n"
        f"لینک ویدیو یا موزیک خود را فرستاده تا دانلود شود.\n"
        f"📊 اعتبار: {max_u - usage} از {max_u}",
        reply_markup=build_main_keyboard()
    )

@router.callback_query(F.data == "ui_home")
async def ui_home(cb: CallbackQuery):
    await cb.message.edit_text("✨ لینک ویدیو یا موزیک را ارسال کنید:", reply_markup=build_main_keyboard())
    await cb.answer()

@router.callback_query(F.data == "ui_cancel")
async def ui_cancel(cb: CallbackQuery):
    await cb.message.edit_text("❌ لغو شد.")
    await cb.answer()

@router.message(F.text)
async def handle_url(message: Message):
    url = message.text.strip()
    if not re.match(r'^https?://', url): return

    allowed, rem = rate_limiter.check(message.from_user.id)
    if not allowed:
        await message.answer(f"⏳ محدودیت دانلود! {rem // 60} دقیقه دیگر سعی کنید.")
        return

    platform = detect_platform(url)
    status_msg = await message.answer("🔍 در حال بررسی لینک...")

    try:
        if platform == 'spotify':
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()
                    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    if m:
                        title = re.sub(r'\s*(song|album|playlist)\s+by\s+', ' ', m.group(1), flags=re.IGNORECASE)
                        url = f"ytsearch1:{title} official audio"
                        platform = 'youtube'

        loop = asyncio.get_running_loop()
        ydl_opts = get_base_opts(config.DOWNLOAD_DIR)
        if platform == 'youtube': ydl_opts.update(get_youtube_opts())

        ydl = yt_dlp.YoutubeDL(ydl_opts)
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        
        url_key = url_store.save(url)
        caption = f"📌 <b>عنوان:</b> {info.get('title', '')[:80]}\n⏱ <b>زمان:</b> {format_duration(info.get('duration', 0))}"
        await status_msg.edit_text(caption, reply_markup=build_media_keyboard(url_key, platform == 'youtube'))

    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text("❌ خطا در پردازش لینک. لطفا مجددا تلاش کنید.")

@router.callback_query(F.data.startswith("dl:"))
async def process_download(cb: CallbackQuery):
    _, mode, url_key = cb.data.split(":")
    url = url_store.get(url_key)
    if not url:
        await cb.answer("❌ منقضی شده است.", show_alert=True)
        return

    await cb.message.edit_text("⏳ در حال دانلود...")
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    ydl_opts = get_base_opts(config.DOWNLOAD_DIR)
    if detect_platform(url) == 'youtube': ydl_opts.update(get_youtube_opts())

    if mode == "audio":
        ydl_opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]})
    elif mode == "1080":
        ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best'
    else:
        ydl_opts['format'] = 'best'

    try:
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(ydl_opts)
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
        filename = ydl.prepare_filename(info)
        if mode == "audio": filename = os.path.splitext(filename)[0] + '.mp3'

        if os.path.exists(filename):
            await cb.message.edit_text("📤 در حال ارسال...")
            if mode == "audio":
                await cb.message.answer_audio(FSInputFile(filename), caption="🆔 @MIKO_Bot")
            else:
                await cb.message.answer_document(FSInputFile(filename), caption="🆔 @MIKO_Bot")
            os.remove(filename)
            await cb.message.delete()
    except Exception as e:
        logger.error(f"Download Error: {e}")
        await cb.message.edit_text("❌ خطا در دانلود فایل.")
    finally:
        await cb.answer()

# ============================================================================
# آپدیت خودکار yt-dlp و وب‌سرور HealthCheck
# ============================================================================
async def auto_update_ytdlp():
    while True:
        await asyncio.sleep(config.AUTO_UPDATE_INTERVAL)
        proc = await asyncio.create_subprocess_exec('pip', 'install', '--upgrade', 'yt-dlp')
        await proc.communicate()

async def handle_ping(request):
    return web.Response(text="MIKO Bot is Live!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    logger.info("🚀 MIKO Bot Starting...")
    asyncio.create_task(auto_update_ytdlp())
    await asyncio.gather(
        dp.start_polling(bot, skip_updates=True),
        start_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
