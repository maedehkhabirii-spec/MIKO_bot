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

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile,
    CallbackQuery, Message
)
from aiogram.enums import ParseMode, ChatAction
from aiogram.exceptions import TelegramAPIError
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError
from aiohttp import web

# ============================================================================
# بارگذاری متغیرهای محیطی و تنظیمات لاگینگ
# ============================================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ متغیر BOT_TOKEN در فایل .env یا تنظیمات سرور یافت نشد.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("MIKO_BOT")

# راه‌اندازی ربات با ساختار جدید Aiogram 3.15+
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ============================================================================
# کلاس پیکربندی ربات
# ============================================================================
@dataclass
class Config:
    RATE_LIMIT: int = 15                   # تعداد دانلود مجاز در بازه زمانی
    TIME_WINDOW: int = 3600                # بازه زمانی محدودیتی (۱ ساعت)
    MAX_FILE_SIZE: int = 48 * 1024 * 1024  # سقف مستقیم تلگرام (۴۸ مگابایت)
    CHUNK_SIZE: int = 40 * 1024 * 1024     # اندازه پارت‌ها برای فایل‌های بزرگ (۴۰ مگابایت)
    DOWNLOAD_TIMEOUT: int = 300            # حداکثر زمان دانلود (۵ دقیقه)
    CACHE_TTL: int = 3600                  # زمان ماندگاری کش (۱ ساعت)
    AUTO_UPDATE_INTERVAL: int = 86400      # بازه زمانی آپدیت yt-dlp (۲۴ ساعت)
    MAX_PLAYLIST_ITEMS: int = 10           # حداکثر نمایش آیتم‌های لیست پخش
    DOWNLOAD_DIR: str = "downloads"
    COOKIES_FILE: str = "cookies.txt"

config = Config()

# ============================================================================
# سیستم مدیریت طول Callback Data (حل مشکل BUTTON_DATA_INVALID)
# ============================================================================
class URLStore:
    """ذخیره‌سازی لینک‌ها با هش کلید کوتاه برای جلوگیری از خطای ۶۴ بایتی تلگرام"""
    def __init__(self):
        self._map: Dict[str, str] = {}

    def save(self, url: str) -> str:
        key = hashlib.md5(url.encode()).hexdigest()[:10]
        self._map[key] = url
        return key

    def get(self, key: str) -> Optional[str]:
        return self._map.get(key)

url_store = URLStore()

# ============================================================================
# سیستم کش هوشمند متاداده‌ها
# ============================================================================
class SmartCache:
    def __init__(self, ttl: int = 3600):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._ttl = ttl

    def _make_key(self, url: str, mode: str) -> str:
        return hashlib.md5(f"{url}:{mode}".encode()).hexdigest()

    def get(self, url: str, mode: str) -> Optional[Dict]:
        key = self._make_key(url, mode)
        if key in self._cache:
            data = self._cache[key]
            if time.time() - data['timestamp'] < self._ttl:
                return data['info']
            else:
                del self._cache[key]
        return None

    def set(self, url: str, mode: str, info: Dict):
        key = self._make_key(url, mode)
        self._cache[key] = {'info': info, 'timestamp': time.time()}
        if len(self._cache) > 200:
            self._cleanup()

    def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self._cache.items() if now - v['timestamp'] > self._ttl]
        for k in expired:
            del self._cache[k]

cache = SmartCache(ttl=config.CACHE_TTL)

# ============================================================================
# سیستم کنترل نرخ درخواست (Rate Limiting)
# ============================================================================
class RateLimiter:
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self._user_requests: Dict[int, List[float]] = {}

    def check(self, user_id: int) -> tuple[bool, int]:
        current_time = time.time()
        if user_id not in self._user_requests:
            self._user_requests[user_id] = []

        self._user_requests[user_id] = [
            t for t in self._user_requests[user_id]
            if current_time - t < self.time_window
        ]

        if len(self._user_requests[user_id]) >= self.max_requests:
            remaining = int(self.time_window - (current_time - self._user_requests[user_id][0]))
            return False, remaining

        self._user_requests[user_id].append(current_time)
        return True, 0

    def get_usage(self, user_id: int) -> tuple[int, int]:
        current_time = time.time()
        if user_id not in self._user_requests:
            return 0, self.max_requests
        active = [t for t in self._user_requests[user_id] if current_time - t < self.time_window]
        return len(active), self.max_requests

rate_limiter = RateLimiter(config.RATE_LIMIT, config.TIME_WINDOW)

# ============================================================================
# توابع ابزاری و کمکی
# ============================================================================
def format_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def format_duration(seconds: int) -> str:
    if not seconds:
        return "نامشخص"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    elif 'soundcloud.com' in url_lower:
        return 'soundcloud'
    elif 'twitter.com' in url_lower or 'x.com' in url_lower:
        return 'twitter'
    elif 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'spotify.com' in url_lower:
        return 'spotify'
    elif 'facebook.com' in url_lower or 'fb.watch' in url_lower:
        return 'facebook'
    else:
        return 'other'

async def split_file(file_path: str, chunk_size: int) -> List[str]:
    chunks = []
    part_num = 1
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            chunk_path = f"{file_path}.part{part_num}"
            with open(chunk_path, 'wb') as cf:
                cf.write(chunk)
            chunks.append(chunk_path)
            part_num += 1
    return chunks

async def cleanup_files(file_paths: List[str]):
    for path in file_paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                logger.info(f"🗑️ فایل پاکسازی شد: {os.path.basename(path)}")
        except Exception as e:
            logger.error(f"❌ خطا در پاکسازی فایل {path}: {e}")

 def get_youtube_opts() -> Dict:
    return {
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web', 'mweb'],
                'player_skip': ['webpage', 'configs'],
                'po_token': ['web+1'],  # تلاش برای دور زدن توکن BOT
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Mode': 'navigate',
        },
        'sleep_interval': 1,      # تاخیر کوتاه برای جلوگیری از مسدودی IP سرور
        'max_sleep_interval': 3,
    }
}

def get_base_opts(download_dir: str) -> Dict:
    opts = {
        'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'nocheckcertificate': True,
    }
    if os.path.exists(config.COOKIES_FILE):
        opts['cookiefile'] = config.COOKIES_FILE
    return opts

# ============================================================================
# ساخت کیبوردهای شیشه‌ای (Inline Keyboards)
# ============================================================================
def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📖 راهنمای استفاده", callback_data="ui_help"),
            InlineKeyboardButton(text="📊 وضعیت حساب", callback_data="ui_stats")
        ],
        [
            InlineKeyboardButton(text="🌐 پلتفرم‌های پشتیبانی شده", callback_data="ui_platforms")
        ]
    ])

def build_media_keyboard(url_key: str, is_yt: bool = True) -> InlineKeyboardMarkup:
    if is_yt:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 کیفیت Full HD (1080p)", callback_data=f"dl:1080:{url_key}"),
                InlineKeyboardButton(text="🎬 کیفیت HD (720p)", callback_data=f"dl:720:{url_key}")
            ],
            [
                InlineKeyboardButton(text="⚡ دانلود سریع (Best)", callback_data=f"dl:best:{url_key}"),
                InlineKeyboardButton(text="🎵 استخراج MP3 (320kbps)", callback_data=f"dl:audio:{url_key}")
            ],
            [
                InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")
            ]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 دانلود بهترین کیفیت", callback_data=f"dl:best:{url_key}"),
                InlineKeyboardButton(text="🎵 دریافت فایل صوتی", callback_data=f"dl:audio:{url_key}")
            ],
            [
                InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")
            ]
        ])

# ============================================================================
# هندلرهای دستورات و کیبورد UI
# ============================================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    first_name = message.from_user.first_name or "کاربر"
    usage, max_usage = rate_limiter.get_usage(message.from_user.id)
    
    text = (
        f"✨ <b>سلام {first_name} عزیز! به MIKO Downloader خوش آمدید.</b>\n\n"
        f"🚀 <b>سریع‌ترین ربات دانلود انواع رسانه و ویدیو</b>\n"
        f"کافیست لینک مورد نظر خود را ارسال کنید تا با بالاترین کیفیت آماده دانلود شود.\n\n"
        f"📊 <b>اعتبار دانلود شما:</b> <code>{max_usage - usage} از {max_usage}</code> دانلود در این ساعت\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"👇 لینک خود را بفرستید یا از دکمه‌های زیر استفاده کنید:"
    )
    await message.answer(text, reply_markup=build_main_keyboard())

@router.callback_query(F.data == "ui_help")
async def ui_help(callback: CallbackQuery):
    text = (
        "📖 <b>راهنمای جامع استفاده از MIKO</b>\n\n"
        "1️⃣ <b>ارسال لینک:</b> آدرس ویدیو یا موزیک را ارسال کنید.\n"
        "2️⃣ <b>انتخاب کیفیت:</b> کیفیت مد نظر (1080p, 720p یا MP3) را انتخاب کنید.\n"
        "3️⃣ <b>دریافت فایل:</b> فایل به‌صورت مستقیم یا پارت‌بندی شده ارسال می‌شود.\n\n"
        "💡 <i>نکته اسپاتیفای: با ارسال لینک موزیک اسپاتیفای، ربات به‌صورت خودکار بهترین نسخه صوتی آن را پیدا کرده و تحویل می‌دهد.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="ui_home")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "ui_stats")
async def ui_stats(callback: CallbackQuery):
    usage, max_usage = rate_limiter.get_usage(callback.from_user.id)
    text = (
        "📊 <b>وضعیت حساب کاربری</b>\n\n"
        f"👤 <b>شناسه کاربری:</b> <code>{callback.from_user.id}</code>\n"
        f"📥 <b>دانلودهای این ساعت:</b> <code>{usage}</code>\n"
        f"🔄 <b>سقف مجاز:</b> <code>{max_usage}</code>\n"
        f"⚡ <b>حالت موتور دانلود:</b> 🟢 فعال و بروز"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="ui_home")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "ui_platforms")
async def ui_platforms(callback: CallbackQuery):
    text = (
        "🌐 <b>پلتفرم‌های پشتیبانی شده:</b>\n\n"
        "• 📺 <b>YouTube:</b> ویدیوها، شورتس و لیست‌پخش\n"
        "• 📸 <b>Instagram:</b> پست، ریلز (Reels)\n"
        "• 🎧 <b>Spotify & SoundCloud:</b> موزیک با کیفیت 320kbps\n"
        "• 🎭 <b>TikTok:</b> دانلود بدون واترمارک\n"
        "• 🐦 <b>X (Twitter) & Facebook & Reddit</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="ui_home")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "ui_home")
async def ui_home(callback: CallbackQuery):
    first_name = callback.from_user.first_name or "کاربر"
    usage, max_usage = rate_limiter.get_usage(callback.from_user.id)
    text = (
        f"✨ <b>سلام {first_name} عزیز! به MIKO Downloader خوش آمدید.</b>\n\n"
        f"🚀 <b>سریع‌ترین ربات دانلود انواع رسانه و ویدیو</b>\n"
        f"کافیست لینک مورد نظر خود را ارسال کنید.\n\n"
        f"📊 <b>اعتبار دانلود شما:</b> <code>{max_usage - usage} از {max_usage}</code>"
    )
    await callback.message.edit_text(text, reply_markup=build_main_keyboard())
    await callback.answer()

@router.callback_query(F.data == "ui_cancel")
async def ui_cancel(callback: CallbackQuery):
    await callback.message.edit_text("❌ <i>عملیات پردازش لغو شد.</i>")
    await callback.answer()

# ============================================================================
# پردازش اصلی لینک ورودی
# ============================================================================
@router.message(F.text)
async def handle_url(message: Message):
    url = message.text.strip()
    user_id = message.from_user.id

    if not re.match(r'^https?://', url):
        return

    allowed, remaining = rate_limiter.check(user_id)
    if not allowed:
        await message.answer(f"⏳ <b>محدودیت دانلود!</b>\nلطفاً {remaining // 60} دقیقه دیگر مجدداً تلاش کنید.")
        return

    platform = detect_platform(url)
    status_msg = await message.answer("🔍 <b>در حال آنالیز و استخراج مشخصات لینک...</b>")

    try:
        if platform == 'spotify':
            await status_msg.edit_text("🎧 <b>شناسایی موزیک اسپاتیفای...</b>\n⏳ <i>جستجوی بهترین نسخه صوتی...</i>")
            search_query = await extract_spotify_info(url)
            if not search_query:
                await status_msg.edit_text("❌ <b>خطا در دریافت اطلاعات اسپاتیفای.</b>")
                return
            url = f"ytsearch1:{search_query}"
            platform = 'youtube'

        cached_info = cache.get(url, 'info')
        if cached_info:
            info = cached_info
        else:
            loop = asyncio.get_running_loop()
            ydl_opts = get_base_opts(config.DOWNLOAD_DIR)
            if platform == 'youtube':
                ydl_opts.update(get_youtube_opts())

            ydl = yt_dlp.YoutubeDL(ydl_opts)
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            cache.set(url, 'info', info)

        if info.get('_type') == 'playlist':
            entries = [e for e in info.get('entries', []) if e][:config.MAX_PLAYLIST_ITEMS]
            if not entries:
                await status_msg.edit_text("❌ <b>لیست پخش خالی یا غیرقابل دسترسی است.</b>")
                return

            keyboard_buttons = []
            for i, entry in enumerate(entries, 1):
                entry_title = entry.get('title', f'ویدیو {i}')[:35]
                entry_url = entry.get('webpage_url') or entry.get('url')
                if entry_url:
                    key = url_store.save(entry_url)
                    keyboard_buttons.append([InlineKeyboardButton(text=f"🎬 {i}. {entry_title}", callback_data=f"dl:best:{key}")])
            
            keyboard_buttons.append([InlineKeyboardButton(text="❌ انصراف", callback_data="ui_cancel")])
            await status_msg.edit_text(
                f"📋 <b>لیست پخش شناسایی شد!</b>\n"
                f"📌 <b>عنوان:</b> {info.get('title', 'Playlist')}\n\n"
                f"👇 آیتم مورد نظر را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )
            return

        title = info.get('title', 'نامشخص')
        duration_str = format_duration(info.get('duration', 0))
        uploader = info.get('uploader', 'نامشخص')
        url_key = url_store.save(url)

        caption = (
            f"🎬 <b>اطلاعات فایل شناسایی شد:</b>\n\n"
            f"📌 <b>عنوان:</b> <code>{title[:90]}</code>\n"
            f"⏱ <b>مدت زمان:</b> <code>{duration_str}</code>\n"
            f"👤 <b>ناشر:</b> <code>{uploader}</code>\n\n"
            f"👇 <b>کیفیت یا فرمت دلخواه را انتخاب کنید:</b>"
        )

        is_yt = (platform == 'youtube')
        await status_msg.edit_text(caption, reply_markup=build_media_keyboard(url_key, is_yt=is_yt))

    except Exception as e:
        logger.error(f"خطای پردازش لینک: {e}")
        await status_msg.edit_text("❌ <b>خطا در آنالیز لینک. ممکن است محتوا خصوصی باشد یا حذف شده باشد.</b>")

async def extract_spotify_info(url: str) -> Optional[str]:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                html = await resp.text()
                match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if match:
                    title = match.group(1)
                    title = re.sub(r'\s*(song|album|playlist)\s+by\s+', ' ', title, flags=re.IGNORECASE)
                    return f"{title} official audio"
    except Exception as e:
        logger.error(f"خطا در استخراج اسپاتیفای: {e}")
    return None

# ============================================================================
# دانلود و ارسال رسانه
# ============================================================================
@router.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery):
    _, mode, url_key = callback.data.split(":")
    url = url_store.get(url_key)

    if not url:
        await callback.answer("❌ نشست دانلود منقضی شده است. مجدداً لینک را بفرستید.", show_alert=True)
        return

    await callback.message.edit_text("⏳ <b>در حال دانلود رسانه... لطفاً شکیبا باشید...</b>")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    download_dir = config.DOWNLOAD_DIR
    os.makedirs(download_dir, exist_ok=True)
    
    platform = detect_platform(url)
    ydl_opts = get_base_opts(download_dir)
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

        if not os.path.exists(filename):
            raise FileNotFoundError("فایل خروجی یافت نشد.")

        files_to_cleanup.append(filename)
        file_size = os.path.getsize(filename)
        file_size_str = format_bytes(file_size)
        title = info.get('title', 'Media File')

        caption = (
            f"✅ <b>دانلود با موفقیت انجام شد!</b>\n\n"
            f"📌 <b>عنوان:</b> {title[:100]}\n"
            f"📦 <b>حجم:</b> <code>{file_size_str}</code>\n\n"
            f"🆔 @MIKO_Bot"
        )

        if file_size <= config.MAX_FILE_SIZE:
            await callback.message.edit_text("📤 <i>در حال آپلود به تلگرام...</i>")
            if mode == "audio":
                await callback.message.answer_audio(
                    audio=FSInputFile(filename),
                    caption=caption,
                    title=title[:50],
                    performer=info.get('uploader', 'MIKO')
                )
            else:
                await callback.message.answer_document(
                    document=FSInputFile(filename),
                    caption=caption
                )
            await callback.message.delete()
        else:
            await callback.message.edit_text(
                f"📦 <b>حجم فایل ({file_size_str}) بیشتر از سقف تلگرام است.</b>\n"
                f"⏳ <i>در حال تقسیم به بخش‌های ۴۰ مگابایتی...</i>"
            )
            chunks = await split_file(filename, config.CHUNK_SIZE)
            files_to_cleanup.extend(chunks)

            for i, chunk_path in enumerate(chunks, 1):
                part_caption = f"{caption}\n🧩 <b>پارت {i} از {len(chunks)}</b>"
                await callback.message.answer_document(
                    document=FSInputFile(chunk_path),
                    caption=part_caption
                )
            await callback.message.delete()

    except asyncio.TimeoutError:
        await callback.message.edit_text("⏰ <b>زمان دانلود به پایان رسید (Timeout).</b>")
    except Exception as e:
        logger.error(f"خطا در دانلود: {e}")
        await callback.message.edit_text("❌ <b>خطا در دانلود یا آپلود فایل.</b>")
    finally:
        await callback.answer()
        asyncio.create_task(cleanup_files(files_to_cleanup))

# ============================================================================
# موتور آپدیت خودکار yt-dlp در پس‌زمینه (هر ۲۴ ساعت)
# ============================================================================
async def auto_update_ytdlp():
    """آپدیت خودکار کتابخانه yt-dlp برای مقابله با تغییرات یوتیوب"""
    while True:
        try:
            await asyncio.sleep(config.AUTO_UPDATE_INTERVAL)
            logger.info("🔄 اجرای فرایند آپدیت خودکار yt-dlp...")
            process = await asyncio.create_subprocess_exec(
                'pip', 'install', '--upgrade', 'yt-dlp', 'bgutil-ytdlp-pot-provider',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                logger.info("✅ کتابخانه yt-dlp با موفقیت آپدیت شد.")
            else:
                logger.error(f"❌ خطا در آپدیت yt-dlp: {stderr.decode()}")
        except Exception as e:
            logger.error(f"❌ خطای سیستم آپدیت خودکار: {e}")

# ============================================================================
# وب سرور داخلی (Ping/Healthcheck Server برای Render)
# ============================================================================
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

# ============================================================================
# اجرای اصلی ربات
# ============================================================================
async def main():
    logger.info("🚀 ربات MIKO با موفقیت راه‌اندازی شد.")
    
    # اجرای تسک آپدیت دوره‌ای در پس‌زمینه
    asyncio.create_task(auto_update_ytdlp())
    
    # اجرای همزمان ربات و وب سرور
    await asyncio.gather(
        dp.start_polling(bot, skip_updates=True),
        start_web_server()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 ربات با دستور کاربر خاموش شد.")
