import os
import asyncio
import time
import logging
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.enums import ParseMode
import yt_dlp
from aiohttp import web

# بارگذاری متغیرهای محیطی
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("لطفاً BOT_TOKEN را در فایل .env تنظیم کنید.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# --- مدیریت محدودیت نرخ (Rate Limiting) ---
# ساختار: {user_id: [timestamp1, timestamp2, ...]}
RATE_LIMIT = 10  # حداکثر تعداد دانلود
TIME_WINDOW = 3600  # بازه زمانی به ثانیه (۱ ساعت)
user_requests = {}

def check_rate_limit(user_id: int) -> bool:
    current_time = time.time()
    if user_id not in user_requests:
        user_requests[user_id] = []
    
    # حذف درخواست‌های قدیمی‌تر از بازه زمانی
    user_requests[user_id] = [t for t in user_requests[user_id] if current_time - t < TIME_WINDOW]
    
    if len(user_requests[user_id]) >= RATE_LIMIT:
        return False
    
    user_requests[user_id].append(current_time)
    return True

# --- توابع کمکی ---
async def split_file(file_path: str, chunk_size: int = 40 * 1024 * 1024) -> list:
    """تقسیم فایل به قطعات ۴۰ مگابایتی برای دور زدن محدودیت تلگرام"""
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

async def cleanup_files(file_paths: list):
    """حذف فایل‌های موقت"""
    for path in file_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"فایل حذف شد: {path}")
        except Exception as e:
            logger.error(f"خطا در حذف فایل {path}: {e}")

# --- هندلرهای ربات ---
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 سلام! من <b>MIKO</b> هستم، دستیار دانلود شما!\n\n"
        "🎬 لینک ویدیو یا صوت مورد نظر خود را از یوتیوب (یا ۱۰۰۰+ سایت دیگر) ارسال کنید.\n\n"
        "⚠️ محدودیت: حداکثر ۱۰ دانلود در ساعت برای هر کاربر.\n\n"
        "✨ شروع کنید و از دانلود لذت ببرید!",
        parse_mode=ParseMode.HTML
    )

@router.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()
    user_id = message.from_user.id

    if not check_rate_limit(user_id):
        await message.answer("⚠️ شما به سقف مجاز دانلود (۱۰ بار در ساعت) رسیده‌اید. لطفاً بعداً تلاش کنید.")
        return

    # ارسال پیام در حال پردازش
    status_msg = await message.answer("⏳ در حال دریافت اطلاعات ویدیو...")

    try:
        # استخراج اطلاعات ویدیو با yt-dlp
        ydl_opts_info = {'quiet': True, 'no_warnings': True}
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts_info).extract_info(url, download=False))
        
        title = info.get('title', 'Unknown')
        thumbnail = info.get('thumbnail', None)

        # ساخت کیبورد انتخاب کیفیت
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 ویدیو (بهترین کیفیت MP4)", callback_data=f"dl_video:{url}")],
            [InlineKeyboardButton(text="🎵 فقط صدا (MP3)", callback_data=f"dl_audio:{url}")],
            [InlineKeyboardButton(text="❌ لغو", callback_data="cancel")]
        ])

        caption = f"📥 <b>{title}</b>\n\nلطفاً فرمت مورد نظر را انتخاب کنید:"
        await status_msg.edit_text(text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"خطا در استخراج اطلاعات: {e}")
        await status_msg.edit_text("❌ خطا در دریافت اطلاعات لینک. لطفاً لینک را بررسی کنید.")

@router.callback_query(F.data.startswith("dl_"))
async def process_download(callback: types.CallbackQuery):
    action, url = callback.data.split(":", 1)
    is_audio = (action == "dl_audio")
    
    await callback.message.edit_text("⏳ در حال دانلود و پردازش فایل... (این کار ممکن است کمی زمان ببرد)")

    download_dir = "downloads"
    os.makedirs(download_dir, exist_ok=True)
    
    # تنظیمات yt-dlp
    if is_audio:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True
        }
    else:
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True
        }

    files_to_cleanup = []
    try:
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(ydl_opts)
        
        # اجرای دانلود در thread جداگانه برای قفل نشدن event loop
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
        
        # پیدا کردن فایل دانلود شده
        filename = ydl.prepare_filename(info)
        if is_audio:
            filename = os.path.splitext(filename)[0] + '.mp3'
            
        if not os.path.exists(filename):
            raise FileNotFoundError("فایل دانلود شده پیدا نشد.")
            
        files_to_cleanup.append(filename)
        file_size = os.path.getsize(filename)

        # ارسال فایل
        if file_size <= 45 * 1024 * 1024:  # حاشیه امنیت زیر ۵۰ مگابایت
            await callback.message.answer_document(
                document=FSInputFile(filename),
                caption=f"✅ {info.get('title', 'فایل')}\n\n🤖 توسط MIKO"
            )
        else:
            await callback.message.answer(f"⚠️ حجم فایل ({file_size // (1024*1024)} MB) زیاد است. در حال تقسیم فایل به بخش‌های ۴۰ مگابایتی...")
            chunks = await split_file(filename, chunk_size=40 * 1024 * 1024)
            files_to_cleanup.extend(chunks)
            
            for i, chunk_path in enumerate(chunks):
                await callback.message.answer_document(
                    document=FSInputFile(chunk_path),
                    caption=f"📦 بخش {i+1} از {len(chunks)}\nفایل اصلی: {info.get('title', 'فایل')}\n\n🤖 توسط MIKO"
                )

        await callback.message.answer("✅ دانلود و ارسال با موفقیت انجام شد! 🎉")

    except Exception as e:
        logger.error(f"خطا در دانلود: {e}")
        await callback.message.answer(f"❌ خطا در فرآیند دانلود: {str(e)}")
    
    finally:
        # پاک‌سازی فایل‌ها در پس‌زمینه
        asyncio.create_task(cleanup_files(files_to_cleanup))
        await callback.answer()

@router.callback_query(F.data == "cancel")
async def cancel_download(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ عملیات لغو شد.")
    await callback.answer()

# --- سرور سبک برای بیدار نگه داشتن Render (UptimeRobot) ---
async def handle_ping(request):
    return web.Response(text="MIKO Bot is alive and running!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("Web server started for UptimeRobot pings.")

# --- اجرای اصلی ---
async def main():
    # اجرای همزمان ربات (Polling) و سرور وب
    await asyncio.gather(
        dp.start_polling(bot, skip_updates=True),
        start_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
