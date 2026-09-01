import os
import asyncio
import time
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
import yt_dlp
from yt_dlp.utils import DownloadError
from aiohttp import web

# بارگذاری متغیرهای محیطی
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("لطفاً BOT_TOKEN را در تنظیمات Render تنظیم کنید.")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# --- مدیریت محدودیت نرخ (Rate Limiting) ---
RATE_LIMIT = 10
TIME_WINDOW = 3600
user_requests = {}

def check_rate_limit(user_id: int) -> bool:
    current_time = time.time()
    if user_id not in user_requests:
        user_requests[user_id] = []
    user_requests[user_id] = [t for t in user_requests[user_id] if current_time - t < TIME_WINDOW]
    if len(user_requests[user_id]) >= RATE_LIMIT:
        return False
    user_requests[user_id].append(current_time)
    return True

# --- توابع کمکی ---
def format_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

async def split_file(file_path: str, chunk_size: int = 40 * 1024 * 1024) -> list:
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
    for path in file_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"🗑️ فایل موقت حذف شد: {os.path.basename(path)}")
        except Exception as e:
            logger.error(f"❌ خطا در حذف فایل {path}: {e}")

# --- هندلرهای ربات ---
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "👋 <b>سلام! به ربات دانلودر MIKO خوش آمدید.</b>\n\n"
        "🎬 من می‌توانم ویدیو و صدا را از <b>یوتیوب، اینستاگرام، توییتر و +۱۰۰۰ سایت دیگر</b> دانلود کنم.\n\n"
        "💡 <b>نحوه استفاده:</b>\n"
        "۱. لینک ویدیو یا پست مورد نظر را برای من بفرستید.\n"
        "۲. فرمت دلخواه (ویدیو یا صدا) را انتخاب کنید.\n"
        "۳. فایل را با بالاترین کیفیت دریافت کنید!\n\n"
        "⚠️ <i>محدودیت: حداکثر ۱۰ دانلود در ساعت برای هر کاربر.</i>"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)

@router.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()
    user_id = message.from_user.id

    if not check_rate_limit(user_id):
        await message.answer("⏳ <b>محدودیت استفاده:</b>\nشما به سقف مجاز (۱۰ دانلود در ساعت) رسیده‌اید. لطفاً کمی صبر کنید.")
        return

    status_msg = await message.answer("🔍 <i>در حال بررسی لینک و دریافت اطلاعات...</i>")

    try:
        ydl_opts_info = {
            'quiet': True, 
            'no_warnings': True,
            'cookiefile': 'cookies.txt' # استفاده از کوکی برای دور زدن محدودیت یوتیوب
        }
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts_info).extract_info(url, download=False))
        
        title = info.get('title', 'عنوان نامشخص')
        duration = info.get('duration', 0)
        duration_str = f"{duration // 60} دقیقه" if duration else "نامشخص"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 دانلود ویدیو (MP4)", callback_data=f"dl_video:{url}")],
            [InlineKeyboardButton(text="🎵 دانلود صدا (MP3)", callback_data=f"dl_audio:{url}")],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel")]
        ])

        caption = (
            f"✅ <b>ویدیو شناسایی شد!</b>\n\n"
            f"📌 <b>عنوان:</b> {title}\n"
            f"⏱ <b>مدت زمان:</b> {duration_str}\n\n"
            f"لطفاً فرمت مورد نظر خود را انتخاب کنید:"
        )
        await status_msg.edit_text(text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    except DownloadError as e:
        logger.error(f"خطای دانلود یوتیوب: {e}")
        await status_msg.edit_text("❌ <b>خطا در دریافت اطلاعات!</b>\n\nدلایل احتمالی:\n• لینک نامعتبر یا شکسته است.\n• ویدیو خصوصی (Private) یا حذف شده است.\n• ویدیو محدودیت سنی دارد.\n\nلطفاً لینک دیگری را امتحان کنید.")
    except Exception as e:
        logger.error(f"خطای عمومی در استخراج اطلاعات: {e}")
        await status_msg.edit_text("❌ یک خطای غیرمنتظره رخ داد. لطفاً دوباره تلاش کنید.")

@router.callback_query(F.data.startswith("dl_"))
async def process_download(callback: types.CallbackQuery):
    action, url = callback.data.split(":", 1)
    is_audio = (action == "dl_audio")
    
    await callback.message.edit_text("⏳ <i>در حال دانلود و پردازش فایل... لطفاً صبر کنید.</i>")
    
    # ارسال اکشن "در حال آپلود" به تلگرام برای جلوگیری از تایم‌اوت کاربر
    await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_document")

    download_dir = "downloads"
    os.makedirs(download_dir, exist_ok=True)
    
    if is_audio:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
            'cookiefile': 'cookies.txt',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'quiet': True,
            'no_warnings': True
        }
    else:
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
            'cookiefile': 'cookies.txt',
            'quiet': True,
            'no_warnings': True
        }

    files_to_cleanup = []
    try:
        loop = asyncio.get_running_loop()
        ydl = yt_dlp.YoutubeDL(ydl_opts)
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
        
        filename = ydl.prepare_filename(info)
        if is_audio:
            filename = os.path.splitext(filename)[0] + '.mp3'
            
        if not os.path.exists(filename):
            raise FileNotFoundError("فایل دانلود شده در مسیر مورد نظر پیدا نشد.")
            
        files_to_cleanup.append(filename)
        file_size = os.path.getsize(filename)
        file_size_str = format_bytes(file_size)
        title = info.get('title', 'فایل دانلودی')

        base_caption = (
            f"✅ <b>دانلود با موفقیت انجام شد!</b>\n\n"
            f"📌 {title}\n"
            f"📦 حجم: {file_size_str}\n"
            f"🤖 <i>قدرت‌گرفته از MIKO</i>"
        )

        if file_size <= 45 * 1024 * 1024:
            await callback.message.answer_document(
                document=FSInputFile(filename),
                caption=base_caption,
                parse_mode=ParseMode.HTML
            )
            await callback.message.answer("🎉 فایل با موفقیت ارسال شد!")
        else:
            await callback.message.answer(f"⚠️ حجم فایل ({file_size_str}) زیاد است. ربات به صورت خودکار آن را به بخش‌های ۴۰ مگابایتی تقسیم می‌کند:")
            chunks = await split_file(filename, chunk_size=40 * 1024 * 1024)
            files_to_cleanup.extend(chunks)
            
            for i, chunk_path in enumerate(chunks):
                chunk_caption = f"{base_caption}\n\n📦 <b>بخش {i+1} از {len(chunks)}</b>"
                await callback.message.answer_document(
                    document=FSInputFile(chunk_path),
                    caption=chunk_caption,
                    parse_mode=ParseMode.HTML
                )
            await callback.message.answer("🎉 تمام بخش‌های فایل ارسال شد!")

    except DownloadError as e:
        logger.error(f"خطا در حین دانلود: {e}")
        await callback.message.answer("❌ <b>خطا در حین دانلود!</b>\nممکن است ارتباط با سرور قطع شده یا ویدیو در حین پردازش حذف شده باشد.")
    except Exception as e:
        logger.error(f"خطای غیرمنتظره در دانلود: {e}")
        await callback.message.answer(f"❌ خطای غیرمنتظره: {str(e)}")
    finally:
        await callback.answer()
        asyncio.create_task(cleanup_files(files_to_cleanup))

@router.callback_query(F.data == "cancel")
async def cancel_download(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ <i>عملیات دانلود لغو شد.</i>")
    await callback.answer()

# --- سرور سبک برای بیدار نگه داشتن Render ---
async def handle_ping(request):
    return web.Response(text="MIKO Bot is alive, healthy, and ready to serve! 🚀")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/healthz', handle_ping)]) # هماهنگ با تنظیمات Advanced رندر
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Web server started on port {port} for UptimeRobot.")

async def main():
    logger.info("🚀 Starting MIKO Bot...")
    await asyncio.gather(
        dp.start_polling(bot, skip_updates=True),
        start_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
