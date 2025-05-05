import os
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from yt_dlp import YoutubeDL

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Store user sessions temporarily
user_sessions = {}

# Define a function to extract formats from the video
def get_video_formats(url):
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "cookiefile": "cookies.txt",  # Automatically use cookies.txt if available
        "writeinfojson": True,  # This writes additional info about the video
    }

    if "instagram.com" in url:
        # Automatically handle cookies extraction
        ydl_opts["cookiesfrombrowser"] = True  # Extract cookies from your browser automatically

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        filtered = []
        for f in formats:
            if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("ext") == "mp4":
                label = f"{f.get('format_note', '')} {f.get('height', '')}p"
                filtered.append((label.strip(), f["format_id"]))
        return filtered[:4], info  # Show 3–4 best options

# Handle /start command
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("🎬 Send me a video link and I'll fetch the formats for you!")

# Handle incoming links
async def handle_link(update: Update, context: CallbackContext):
    url = update.message.text.strip()
    try:
        formats, info = get_video_formats(url)
        if not formats:
            await update.message.reply_text("❌ No downloadable formats found.")
            return

        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"{url}|{fmt_id}")]
            for label, fmt_id in formats
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Save session
        user_sessions[update.effective_user.id] = {"url": url}

        await update.message.reply_text("📥 Choose a quality:", reply_markup=reply_markup)
    except Exception as e:
        logger.error(str(e))
        await update.message.reply_text("❌ Couldn't fetch video info. Make sure the link is correct.")

# Handle button selection
async def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if "|" not in data:
        await query.edit_message_text("❌ Invalid format selection.")
        return

    url, fmt_id = data.split("|")
    output_path = f"{user_id}_{fmt_id}.mp4"

    try:
        ydl_opts = {
            "format": fmt_id,
            "outtmpl": output_path,
            "quiet": True,
            "noplaylist": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }

        cookie_file = "instagram.com_cookies.txt"
        if "instagram.com" in url and os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Check file size
        file_size = os.path.getsize(output_path)
        if file_size > 50 * 1024 * 1024:
            await query.edit_message_text("📦 File is large, sending in chunks...")
            await split_and_send_large_file(context, query.message.chat_id, output_path)
        else:
            await context.bot.send_video(chat_id=query.message.chat_id, video=open(output_path, "rb"))

        os.remove(output_path)
    except Exception as e:
        logger.error(str(e))
        await query.edit_message_text("❌ Error during download.")

# Split large file into 50MB chunks using FFmpeg
async def split_and_send_large_file(context, chat_id, filepath):
    import subprocess

    base_name = os.path.splitext(filepath)[0]
    chunk_dir = f"{base_name}_chunks"
    os.makedirs(chunk_dir, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-i", filepath,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", "60",
        f"{chunk_dir}/out%03d.mp4"
    ]
    subprocess.run(cmd, check=True)

    for fname in sorted(os.listdir(chunk_dir)):
        full_path = os.path.join(chunk_dir, fname)
        await context.bot.send_video(chat_id=chat_id, video=open(full_path, "rb"))
        os.remove(full_path)
    os.rmdir(chunk_dir)
    os.remove(filepath)

# Main bot entry
if __name__ == "__main__":
    import asyncio
    import os
    from telegram.ext import Application, ApplicationBuilder
    token = os.getenv("BOT_TOKEN")  # Ensure this is actually set
    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_handler(CallbackQueryHandler(button_callback))

    print("Bot is running...")
    application.run_polling()
