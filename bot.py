import os
import asyncio
import time
import logging
import re
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
# لاگینگ و راه‌اندازی ربات
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("MIKO_BOT")

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.critical("❌ متغیر BOT_TOKEN یافت نشد! لطفا در تنظیمات Render آن را اضافه کنید.")
    time.sleep(10)
    exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ============================================================================
# تنظیمات عمومی پروژه
# ============================================================================
@dataclass
class Config:
    RATE_LIMIT: int = 15
    TIME_WINDOW: int = 3600
    MAX_FILE_SIZE: int = 48 * 1024 * 1024   # سقف مستقیم تلگرام (۴۸ مگابایت)
    CHUNK_SIZE: int = 40 * 1024 * 1024      # پارت‌های ۴۰ مگابایتی برای فایل‌های بزرگ
    DOWNLOAD_TIMEOUT: int = 300
    CACHE_TTL: int = 3600
    AUTO_UPDATE_INTERVAL: int = 86400       # بازه آپدیت yt-dlp (۲۴ ساعت)
    MAX_PLAYLIST_ITEMS: int = 10
    DOWNLOAD_DIR: str = "downloads"

config = Config()

# ============================================================================
# کلاس‌های ابزاری (کوتاه‌کننده لینک، کش و Rate Limiter)
# ============================================================================
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
# توابع کمکی و تنظیمات پیشرفته yt-dlp
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
    if 'soundcloud.com' in u: return 'soundcloud'
    return 'other'

def get_base_opts(download_dir: str) -> Dict:
    opts = {
        'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Mode': 'navigate',
        }
    }
    
    # چک کردن فایل کوکی در محیط محلی یا در Secret Files سرور Render
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
    elif os.path.exists("/etc/secrets/cookies.txt"):
        opts['cookiefile'] = "/etc/secrets/cookies.txt"

    return opts

def get_youtube_opts() -> Dict:
    return {
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'mweb', 'android'],
                'player_skip': ['webpage', 'configs'],
            }
        }
    }

async def split_file(file_path: str, chunk_size: int) -> List[str]:
    chunks = []
    part_num = 1
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk: break
            chunk_path = f"{file_path}.part{part_num}"
            with open(chunk_path, 'wb') as cf:
                cf.write(chunk)
            chunks.append(chunk_path)
            part_num += 1
    return chunks

# ============================================================================
# کیبوردهای شیشه‌ای (Inline Keyboards)
# ============================================================================
def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 راهنما", callback_data="ui_help"), InlineKeyboardButton(text="📊 وضعیت حساب", callback_data="ui_stats")],
        [InlineKeyboardButton(text="🌐 پلتفرم‌های پشتیبانی شده", callback_data="ui_platforms")]
    ])

def build_media_keyboard(url_key: str, is_yt: bool = True) -> InlineKeyboardMarkup:
    if is_yt:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 کیفیت 1080p", callback_data=f"dl:1080:{url_key}"), InlineKeyboardButton(text="🎬 کیفیت 720p", callback_data=f"dl:720:{url_key}")],
            [InlineKeyboardButton(text="⚡ بهترین کیفیت (Best)", callback_data=f"dl:best:{url_key}"), InlineKeyboardButton(text="🎵 استخراج MP3", callback_data=f"dl:audio:{url_key}")],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 دانلود با بهترین کیفیت", callback_data=f"dl:best:{url_key}"), InlineKeyboardButton(text="🎵 دریافت فایل صوتی", callback_data=f"dl:audio:{url_key}")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")]
    ])

# ============================================================================
# هندلرهای دستورات و منوهای UI
# ============================================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    usage, max_u = rate_limiter.get_usage(message.from_user.id)
    await message.answer(
        f"✨ <b>سلام {message.from_user.first_name}! به MIKO Downloader خوش آمدید.</b>\n\n"
        f"لینک ویدیو یا موزیک مورد نظر خود را ارسال کنید تا آماده دانلود شود.\n"
        f"📊 <b>اعتبار دانلود شما:</b> <code>{max_u - usage} از {max_u}</code> دانلود در این ساعت",
        reply_markup=build_main_keyboard()
    )

@router.callback_query(F.data == "ui_help")
async def ui_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "📖 <b>راهنمای استفاده:</b>\n\n"
        "1️⃣ لینک ویدیو (یوتیوب، اینستاگرام، تیک‌تاک و ...) یا موزیک (اسپاتیفای، ساندکلاد) را ارسال کنید.\n"
        "2️⃣ فرمت یا کیفیت مورد نظر را انتخاب کنید.\n"
        "3️⃣ فایل دانلود شده را تحویل بگیرید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ui_home")]])
    )
    await cb.answer()

@router.callback_query(F.data == "ui_stats")
async def ui_stats(cb: CallbackQuery):
    usage, max_u = rate_limiter.get_usage(cb.from_user.id)
    await cb.message.edit_text(
        f"📊 <b>وضعیت حساب:</b>\n\n"
        f"👤 شناسه: <code>{cb.from_user.id}</code>\n"
        f"📥 دانلودهای این ساعت: <code>{usage} از {max_u}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ui_home")]])
    )
    await cb.answer()

@router.callback_query(F.data == "ui_platforms")
async def ui_platforms(cb: CallbackQuery):
    await cb.message.edit_text(
        "🌐 <b>پلتفرم‌های پشتیبانی شده:</b>\n\n"
        "• YouTube (ویدیو، شورتس، لیست‌پخش)\n"
        "• Instagram (پست، ریلز)\n"
        "• Spotify & SoundCloud\n"
        "• TikTok, X (Twitter), Facebook",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ui_home")]])
    )
    await cb.answer()

@router.callback_query(F.data == "ui_home")
async def ui_home(cb: CallbackQuery):
    await cb.message.edit_text("✨ لینک ویدیو یا موزیک خود را ارسال کنید:", reply_markup=build_main_keyboard())
    await cb.answer()

@router.callback_query(F.data == "ui_cancel")
async def ui_cancel(cb: CallbackQuery):
    await cb.message.edit_text("❌ عملیات لغو شد.")
    await cb.answer()

# ============================================================================
# پردازش اصلی لینک
# ============================================================================
@router.message(F.text)
async def handle_url(message: Message):
    url = message.text.strip()
    if not re.match(r'^https?://', url): return

    allowed, rem = rate_limiter.check(message.from_user.id)
    if not allowed:
        await message.answer(f"⏳ <b>محدودیت دانلود!</b> لطفاً {rem // 60} دقیقه دیگر تلاش کنید.")
        return

    platform = detect_platform(url)
    status_msg = await message.answer("🔍 <b>در حال آنالیز و استخراج مشخصات لینک...</b>")

    try:
        # پردازش لینک‌های اسپاتیفای
        if platform == 'spotify':
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True) as resp:
                    html = await resp.text()
                    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    if m:
                        title = re.sub(r'\s*(song|album|playlist)\s+by\s+', ' ', m.group(1), flags=re.IGNORECASE)
                        url = f"ytsearch1:{title} official audio"
                        platform = 'youtube'

        loop = asyncio.get_running_loop()
        ydl_opts = get_base_opts(config.DOWNLOAD_DIR)
        if platform == 'youtube':
            ydl_opts.update(get_youtube_opts())

        ydl = yt_dlp.YoutubeDL(ydl_opts)
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        
        url_key = url_store.save(url)
        title = info.get('title', 'نامشخص')
        duration = format_duration(info.get('duration', 0))

        caption = (
            f"🎬 <b>مشخصات فایل شناسایی شد:</b>\n\n"
            f"📌 <b>عنوان:</b> <code>{title[:90]}</code>\n"
            f"⏱ <b>مدت زمان:</b> <code>{duration}</code>\n\n"
            f"👇 کیفیت یا فرمت مورد نظر را انتخاب کنید:"
        )
        await status_msg.edit_text(caption, reply_markup=build_media_keyboard(url_key, platform == 'youtube'))

    except Exception as e:
        logger.error(f"خطا در پردازش لینک: {e}")
        await status_msg.edit_text("❌ <b>خطا در پردازش لینک. ممکن است محتوا غیرقابل دسترس یا خصوصی باشد.</b>")

# ============================================================================
# دانلود و ارسال فایل
# ============================================================================
@router.callback_query(F.data.startswith("dl:"))
async def process_download(cb: CallbackQuery):
    _, mode, url_key = cb.data.split(":")
    url = url_store.get(url_key)
    if not url:
        await cb.answer("❌ این نشست منقضی شده است. مجدداً لینک را بفرستید.", show_alert=True)
        return

    await cb.message.edit_text("⏳ <b>در حال دانلود رسانه... لطفاً شکیبا باشید...</b>")
    await bot.send_chat_action(chat_id=cb.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    ydl_opts = get_base_opts(config.DOWNLOAD_DIR)
    platform = detect_platform(url)
    if platform == 'youtube':
        ydl_opts.update(get_youtube_opts())

    if mode == "audio":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
        })
    elif mode == "1080":
        ydl_opts['format'] = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best'
    elif mode == "720":
        ydl_opts['format'] = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best'
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    files_to_cleanup = []
    try:
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(ydl_opts)

        info = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True)),
            timeout=config.DOWNLOAD_TIMEOUT
        )

        filename = ydl.prepare_filename(info)
        if mode == "audio":
            filename = os.path.splitext(filename)[0] + '.mp3'

        if not os.path.exists(filename):
            base_path = os.path.splitext(filename)[0]
            for ext in ['.mp4', '.mkv', '.webm', '.mp3']:
                if os.path.exists(base_path + ext):
                    filename = base_path + ext
                    break

        files_to_cleanup.append(filename)
        file_size = os.path.getsize(filename)
        size_str = format_bytes(file_size)
        title = info.get('title', 'Media File')

        caption = f"✅ <b>دانلود انجام شد!</b>\n\n📌 <b>عنوان:</b> {title[:90]}\n📦 <b>حجم:</b> <code>{size_str}</code>\n\n🆔 @MIKO_Bot"

        if file_size <= config.MAX_FILE_SIZE:
            await cb.message.edit_text("📤 <i>در حال آپلود به تلگرام...</i>")
            if mode == "audio":
                await cb.message.answer_audio(FSInputFile(filename), caption=caption, title=title[:50])
            else:
                await cb.message.answer_document(FSInputFile(filename), caption=caption)
            await cb.message.delete()
        else:
            await cb.message.edit_text(f"📦 <b>حجم فایل ({size_str}) بیشتر از سقف تلگرام است.</b>\n⏳ <i>تقسیم به پارت‌های ۴۰ مگابایتی...</i>")
            chunks = await split_file(filename, config.CHUNK_SIZE)
            files_to_cleanup.extend(chunks)

            for i, chunk_path in enumerate(chunks, 1):
                part_caption = f"{caption}\n🧩 <b>پارت {i} از {len(chunks)}</b>"
                await cb.message.answer_document(FSInputFile(chunk_path), caption=part_caption)
            await cb.message.delete()

    except Exception as e:
        logger.error(f"خطا در دانلود: {e}")
        await cb.message.edit_text("❌ <b>خطا در دانلود یا آپلود فایل.</b>")
    finally:
        await cb.answer()
        for fpath in files_to_cleanup:
            if fpath and os.path.exists(fpath):
                try: os.remove(fpath)
                except: pass

# ============================================================================
# آپدیت دوره‌ای yt-dlp و وب‌سرور HealthCheck
# ============================================================================
async def auto_update_ytdlp():
    while True:
        await asyncio.sleep(config.AUTO_UPDATE_INTERVAL)
        logger.info("🔄 اجرای خودکار آپدیت yt-dlp...")
        proc = await asyncio.create_subprocess_exec('pip', 'install', '--upgrade', 'yt-dlp')
        await proc.communicate()

async def handle_ping(request):
    return web.Response(text="MIKO Bot Status: ONLINE 🚀")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 سرور HealthCheck روی پورت {port} فعال شد.")

async def main():
    logger.info("🚀 MIKO Bot راه‌اندازی شد.")
    asyncio.create_task(auto_update_ytdlp())
    await asyncio.gather(
        dp.start_polling(bot, skip_updates=True),
        start_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
