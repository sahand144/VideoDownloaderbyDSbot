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

# === Helpers ===
def get_formats(url):
    ydl_opts = {"quiet": True, "listformats": True}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        filtered = [f for f in formats if f.get("filesize") and f["ext"] == "mp4"]
        options = []
        seen = set()
        for f in filtered:
            height = f.get("height")
            format_id = f.get("format_id")
            label = f"{height}p" if height else f["format"]
            if label not in seen:
                seen.add(label)
                options.append((label, format_id))
        return options[:4], info

def format_size(bytes):
    return round(bytes / (1024 * 1024), 2)

def split_video(path):
    output_files = []
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    duration = float(probe.stdout)
    chunk_duration = 240  # seconds (4min) ~= 50MB for most formats
    n_chunks = int(duration // chunk_duration) + 1

    for i in range(n_chunks):
        out_file = f"{path}_part{i+1}.mp4"
        subprocess.run([
            "ffmpeg", "-i", path, "-ss", str(i * chunk_duration), "-t",
            str(chunk_duration), "-c", "copy", out_file, "-y"
        ])
        output_files.append(out_file)
    return output_files

# === Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a video link to download!")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    keyboard = []
    try:
        options, info = get_formats(url)

        # Warn for long videos
        duration = info.get("duration", 0)
        if duration > 7200:
            await update.message.reply_text("❌ Extremely long video - please try a shorter one.")
            return
        elif duration > 1800:
            await update.message.reply_text("⚠️ Long video - may take extra time.")

        for label, fmt_id in options:
            keyboard.append([InlineKeyboardButton(label, callback_data=f"{url}|{fmt_id}")])
        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select video quality:", reply_markup=markup)
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("Failed to fetch video formats. Please check the link.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    url, fmt_id = data[0], data[1]

    file_id = str(uuid.uuid4())
    output_path = f"{DOWNLOAD_DIR}/{file_id}.mp4"

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    ydl_opts = {
        "format": fmt_id,
        "outtmpl": output_path,
        "quiet": True,
    }

    try:
        await query.edit_message_text("📥 Downloading...")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        size = os.path.getsize(output_path) / (1024 * 1024)
        if size > MAX_SIZE_MB:
            await query.message.reply_text(f"Video size: {format_size(size)}MB – splitting into chunks...")
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
        await query.message.reply_text("❌ Error during download or upload.")

# === Main ===
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(button))
    app.run_polling()
