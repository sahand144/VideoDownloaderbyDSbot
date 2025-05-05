import os
import subprocess
import logging
import uuid
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from yt_dlp import YoutubeDL

# === Setup ===
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = "downloads"
MAX_SIZE_MB = 50

# === Helper Functions ===
def get_video_formats(url):
    """Extracts mp4 formats with resolution and audio."""
    ydl_opts = {"quiet": True}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        filtered = []
        for f in formats:
            if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("ext") == "mp4":
                label = f"{f.get('format_note', '')} {f.get('height', '')}p"
                filtered.append((label.strip(), f["format_id"]))
        return filtered[:4], info

def format_size(bytes):
    return round(bytes / (1024 * 1024), 2)

def split_video(path):
    """Splits video into ~4-minute chunks if size > 50MB."""
    output_files = []
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    duration = float(probe.stdout)
    chunk_duration = 240  # seconds
    n_chunks = int(duration // chunk_duration) + 1

    for i in range(n_chunks):
        out_file = f"{path}_part{i+1}.mp4"
        subprocess.run([
            "ffmpeg", "-i", path, "-ss", str(i * chunk_duration), "-t",
            str(chunk_duration), "-c", "copy", out_file, "-y"
        ])
        output_files.append(out_file)
    return output_files

# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 Send me a video link to download!")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    try:
        options, info = get_video_formats(url)

        if not options:
            await update.message.reply_text("❌ No suitable formats found.")
            return

        duration = info.get("duration", 0)
        if duration > 7200:
            await update.message.reply_text("❌ Video too long (>2 hours). Try a shorter one.")
            return
        elif duration > 1800:
            await update.message.reply_text("⚠️ This is a long video. It may take longer to process.")

        context.user_data["video_url"] = url
        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"{url}|{fmt_id}")]
            for label, fmt_id in options
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("✅ Choose video quality:", reply_markup=markup)

    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Couldn't fetch video info. Make sure the link is correct.")

async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        url, fmt_id = query.data.split("|")
        file_id = str(uuid.uuid4())
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        output_path = f"{DOWNLOAD_DIR}/{file_id}.mp4"

        ydl_opts = {
            "format": fmt_id,
            "outtmpl": output_path,
            "quiet": True,
        }

        await query.edit_message_text("📥 Downloading your selected quality...")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        size = os.path.getsize(output_path) / (1024 * 1024)
        if size > MAX_SIZE_MB:
            await query.message.reply_text(f"📦 Video size: {format_size(size)}MB. Splitting into chunks...")
            chunks = split_video(output_path)
            for chunk in chunks:
                with open(chunk, "rb") as f:
                    await query.message.reply_video(f)
                os.remove(chunk)
        else:
            with open(output_path, "rb") as f:
                await query.message.reply_video(f)
        os.remove(output_path)

    except Exception as e:
        logging.error(e)
        await query.message.reply_text("❌ Error downloading or sending the video.")

# === Main Runner ===
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))

    app.run_polling()
