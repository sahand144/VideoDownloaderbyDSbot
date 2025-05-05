import asyncio
import os
import logging
from datetime import datetime, timedelta
from tempfile import gettempdir

import ffmpeg
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
import yt_dlp

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit
CHUNK_DURATION = 600  # 10 minutes per chunk
LONG_VIDEO_DURATION = 30 * 60  # 30 minutes
VERY_LONG_DURATION = 120 * 60  # 2 hours
QUALITY_TIMEOUT = 30  # seconds to choose quality
TEMP_DIR = os.path.join(gettempdir(), "video_bot")
os.makedirs(TEMP_DIR, exist_ok=True)


class VideoProcessor:
    @staticmethod
    def get_formats_info(url: str) -> tuple:
        """Get available formats and video info."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info, ydl.list_formats(info)

    @staticmethod
    def select_best_format(formats: list) -> str:
        """Automatically select the best balanced format."""
        preferred = []
        for f in formats:
            # Skip formats without audio (unless audio-only)
            if f.get('acodec') == 'none' and f.get('vcodec') != 'none':
                continue
                
            # Prefer mp4 container
            if f.get('ext') == 'mp4':
                preferred.append(f)
                
        if not preferred:
            return "best"
            
        # Find highest resolution under 1080p
        for res in ['1080', '720', '480', '360']:
            for f in preferred:
                if res in f.get('format_note', ''):
                    return f['format_id']
        return "best"

    @staticmethod
    def download_video(url: str, format_id: str) -> str:
        """Download video with specified format."""
        ydl_opts = {
            "format": format_id,
            "outtmpl": os.path.join(TEMP_DIR, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message."""
    welcome_text = """
    🎬 *Video Downloader Bot* 🎬

    Send me a video link from:
    - YouTube, Twitter, Instagram, etc.

    I'll automatically choose the best quality if you don't select one within 30 seconds.

    ⚠️ Note:
    - Very long videos (>2h) may fail
    - Some sites block downloads
    """
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming URLs."""
    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("Please send a valid URL starting with http:// or https://")
        return

    try:
        # Get video info and formats
        info, formats = VideoProcessor.get_formats_info(url)
        duration = info.get('duration', 0)
        
        # Check video length
        if duration > VERY_LONG_DURATION:
            await update.message.reply_text("❌ Videos longer than 2 hours are not supported")
            return
        elif duration > LONG_VIDEO_DURATION:
            await update.message.reply_text("⚠️ Note: This is a long video (30+ mins), processing may take extra time")

        # Store basic info in context
        context.user_data['video_info'] = {
            'url': url,
            'title': info.get('title', 'video')[:100],
            'duration': duration,
            'formats': formats,
            'best_format': VideoProcessor.select_best_format(formats)
        }

        # Show quality options if multiple exist
        await present_quality_options(update, context)

    except Exception as e:
        logger.error(f"URL handling error: {e}")
        await update.message.reply_text("❌ Could not process this URL. It may be invalid or unsupported.")


async def present_quality_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available quality options to user."""
    formats = context.user_data['video_info']['formats']
    url = context.user_data['video_info']['url']
    
    # Group formats by type
    video_formats = []
    audio_formats = []
    
    for f in formats:
        if not f.get('filesize') or f.get('filesize', 0) > 500*1024*1024:  # Skip >500MB
            continue
            
        if f.get('vcodec') != 'none':
            video_formats.append(f)
        elif f.get('acodec') != 'none':
            audio_formats.append(f)
    
    # Create buttons
    buttons = []
    for f in video_formats[:4]:  # Show max 4 video options
        res = f.get('format_note', f.get('height', '?'))
        buttons.append([
            InlineKeyboardButton(
                f"🎥 {res}",
                callback_data=f"format_{f['format_id']}")
        ])
    
    for f in audio_formats[:2]:  # Show max 2 audio options
        abr = f.get('abr', '?') or f.get('tbr', '?')
        buttons.append([
            InlineKeyboardButton(
                f"🔊 Audio ({abr}kbps)",
                callback_data=f"format_{f['format_id']}")
        ])
    
    # Add default option
    buttons.append([
        InlineKeyboardButton(
            "⚡ Auto Select Best",
            callback_data="format_auto")
    ])
    
    # Send message with buttons
    message = await update.message.reply_text(
        "Choose video quality (or wait 30s for auto-select):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )  # THIS WAS THE MISSING PARENTHESIS
    
    # Set timeout for auto-selection
    context.job_queue.run_once(
        auto_select_quality,
        QUALITY_TIMEOUT,
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        data={'message_id': message.message_id}
    )


async def auto_select_quality(context: ContextTypes.DEFAULT_TYPE):
    """Handle quality selection timeout."""
    job = context.job
    try:
        await context.bot.edit_message_text(
            "⏳ Selecting best quality automatically...",
            chat_id=job.chat_id,
            message_id=job.data['message_id']
        )
        await process_download(
            context.bot,
            job.chat_id,
            job.user_id,
            format_id="auto"
        )
    except Exception as e:
        logger.error(f"Auto-select error: {e}")
        await context.bot.send_message(
            job.chat_id,
            "❌ Timed out waiting for quality selection. Please try again."
        )


async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quality selection callback."""
    query = update.callback_query
    await query.answer()
    
    format_id = query.data.replace("format_", "")
    if format_id == "auto":
        format_id = context.user_data['video_info']['best_format']
    
    await query.edit_message_text(
        f"⏳ Downloading with selected quality..."
    )
    
    await process_download(
        context.bot,
        query.message.chat_id,
        query.from_user.id,
        format_id=format_id
    )


async def process_download(bot, chat_id, user_id, format_id):
    """Process video download with specified format."""
    try:
        # Get stored video info
        context = ContextTypes.DEFAULT_TYPE.context_class(ApplicationBuilder().build())
        context.user_data = context.application.user_data[user_id]
        video_info = context.user_data['video_info']
        
        # Download the video
        filepath = VideoProcessor.download_video(
            video_info['url'],
            format_id if format_id != "auto" else video_info['best_format']
        )
        
        # Handle file size and send
        file_size = os.path.getsize(filepath)
        if file_size <= MAX_FILE_SIZE:
            await send_single_video(bot, chat_id, filepath, video_info['title'])
        else:
            await split_and_send_video(bot, chat_id, filepath, video_info['title'], video_info['duration'])
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        await bot.send_message(chat_id, f"❌ Download failed: {str(e)}")
    finally:
        if 'filepath' in locals() and os.path.exists(filepath):
            os.remove(filepath)


async def send_single_video(bot, chat_id, filepath, title):
    """Send single video file."""
    await bot.send_chat_action(chat_id, "upload_video")
    with open(filepath, "rb") as f:
        await bot.send_video(
            chat_id=chat_id,
            video=InputFile(f),
            caption=f"🎥 {title}",
            supports_streaming=True,
        )


async def split_and_send_video(bot, chat_id, filepath, title, duration):
    """Split and send large video."""
    await bot.send_message(chat_id, "✂️ Video is too large - splitting into parts...")
    
    chunk_dir = os.path.join(TEMP_DIR, f"chunks_{datetime.now().timestamp()}")
    os.makedirs(chunk_dir, exist_ok=True)
    
    try:
        # Split with FFmpeg
        base_output = os.path.join(chunk_dir, "part_%03d.mp4")
        ffmpeg.input(filepath).output(
            base_output,
            codec="copy",
            f="segment",
            segment_time=CHUNK_DURATION,
            reset_timestamps=1,
        ).run(quiet=True, overwrite_output=True)
        
        # Send chunks
        chunks = sorted([f for f in os.listdir(chunk_dir) if f.startswith("part_")])
        for i, chunk in enumerate(chunks, 1):
            await bot.send_chat_action(chat_id, "upload_video")
            with open(os.path.join(chunk_dir, chunk), "rb") as f:
                await bot.send_video(
                    chat_id=chat_id,
                    video=InputFile(f),
                    caption=f"🎥 {title} (Part {i}/{len(chunks)})",
                    supports_streaming=True,
                )
        
        await bot.send_message(chat_id, "✅ All parts sent successfully!")
    finally:
        # Cleanup
        if os.path.exists(chunk_dir):
            for f in os.listdir(chunk_dir):
                os.remove(os.path.join(chunk_dir, f))
            os.rmdir(chunk_dir)


def main():
    """Start the bot."""
    application = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(quality_callback, pattern="^format_"))
    
    # Start polling
    application.run_polling()


if __name__ == "__main__":
    main()
