import os
import uuid
import requests
import telebot
import internetarchive as ia

TELEGRAM_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"
IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

bot = telebot.TeleBot(TELEGRAM_TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 Bot is ready! Send me a direct video link or forward a video to upload directly to Internet Archive.",
    )


@bot.message_handler(content_types=["video", "document"])
def handle_video_file(message):
    try:
        status = bot.reply_to(
            message, "📥 Downloading video from Telegram..."
        )

        file_id = None
        if message.video:
            file_id = message.video.file_id
        elif message.document:
            file_id = message.document.file_id

        if not file_id:
            bot.edit_message_text(
                "❌ No valid video found.",
                chat_id=message.chat.id,
                message_id=status.message_id,
            )
            return

        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        item_id = f"tg_video_{uuid.uuid4().hex[:8]}"
        file_path = f"/tmp/{item_id}.mp4"

        with open(file_path, "wb") as new_file:
            new_file.write(downloaded_file)

        bot.edit_message_text(
            "🚀 Uploading to Internet Archive...",
            chat_id=message.chat.id,
            message_id=status.message_id,
        )

        item = ia.get_item(item_id)
        item.upload(
            file_path,
            metadata={"title": f"Upload {item_id}", "mediatype": "movies"},
            access_key=IA_ACCESS,
            secret_key=IA_SECRET,
        )

        if os.path.exists(file_path):
            os.remove(file_path)

        bot.edit_message_text(
            f"✅ Upload Complete!\nhttps://archive.org/details/{item_id}",
            chat_id=message.chat.id,
            message_id=status.message_id,
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")


@bot.message_handler(func=lambda msg: True)
def handle_link_url(message):
    url = message.text.strip()
    if not url.startswith("http"):
        return

    try:
        status = bot.reply_to(
            message, "📥 Downloading video from link..."
        )
        item_id = f"url_video_{uuid.uuid4().hex[:8]}"
        file_path = f"/tmp/{item_id}.mp4"

        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)

        bot.edit_message_text(
            "🚀 Uploading to Internet Archive...",
            chat_id=message.chat.id,
            message_id=status.message_id,
        )

        item = ia.get_item(item_id)
        item.upload(
            file_path,
            metadata={"title": f"Upload {item_id}", "mediatype": "movies"},
            access_key=IA_ACCESS,
            secret_key=IA_SECRET,
        )

        if os.path.exists(file_path):
            os.remove(file_path)

        bot.edit_message_text(
            f"✅ Upload Complete!\nhttps://archive.org/details/{item_id}",
            chat_id=message.chat.id,
            message_id=status.message_id,
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")


if __name__ == "__main__":
    print("Bot is starting cleanly...")
    bot.infinity_polling()
