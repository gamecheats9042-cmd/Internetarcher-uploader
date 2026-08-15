import os
import sys
import uuid
import subprocess

# --- AUTO INSTALL PACKAGES (NO requirements.txt NEEDED) ---
packages = ["python-telegram-bot==20.8", "internetarchive", "requests"]
for pkg in packages:
    name = pkg.split("==")[0]
    try:
        __import__(name)
    except ImportError:
        print(f"Installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

# --- IMPORTS ---
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import internetarchive as ia

# --- YOUR KEYS HARDCODED ---
TELEGRAM_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"
IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bot is ready! Send me a direct video link or forward a video to upload directly to Internet Archive."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("📥 Downloading video from Telegram...")
    video = update.message.video or update.message.document
    tg_file = await video.get_file()

    item_id = f"tg_video_{uuid.uuid4().hex[:8]}"
    file_path = f"/tmp/{item_id}.mp4"

    try:
        await tg_file.download_to_drive(file_path)
        await status.edit_text("🚀 Uploading to Internet Archive...")

        item = ia.get_item(item_id)
        item.upload(
            file_path,
            metadata={"title": f"Upload {item_id}", "mediatype": "movies"},
            access_key=IA_ACCESS,
            secret_key=IA_SECRET,
        )
        await status.edit_text(f"✅ Upload Complete!\nhttps://archive.org/details/{item_id}")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        return

    status = await update.message.reply_text("📥 Downloading video from link...")
    item_id = f"url_video_{uuid.uuid4().hex[:8]}"
    file_path = f"/tmp/{item_id}.mp4"

    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)

        await status.edit_text("🚀 Uploading to Internet Archive...")

        item = ia.get_item(item_id)
        item.upload(
            file_path,
            metadata={"title": f"Upload {item_id}", "mediatype": "movies"},
            access_key=IA_ACCESS,
            secret_key=IA_SECRET,
        )
        await status.edit_text(f"✅ Upload Complete!\nhttps://archive.org/details/{item_id}")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


if __name__ == "__main__":
    print("Bot is starting...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.run_polling()
