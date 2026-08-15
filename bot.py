import os
import uuid
import time
import threading
import requests
import telebot
import internetarchive as ia
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- KEYS ---
TELEGRAM_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"
IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

# --- DUMMY WEB SERVER FOR RENDER ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html = "<h1>Bot is Active and Running!</h1>"
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# --- COMMANDS ---
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.send_message(
        message.chat.id,
        "👋 **Bot is Online!**\n\nSend me a direct video link (.mp4) or forward a video to upload it to Internet Archive.",
        parse_mode="Markdown"
    )

# --- FORWARDED VIDEO / FILE ---
@bot.message_handler(content_types=["video", "document"])
def handle_video_file(message):
    status = bot.reply_to(message, "📥 Downloading video from Telegram...")
    try:
        file_id = message.video.file_id if message.video else message.document.file_id
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
        bot.edit_message_text(f"❌ Error: {str(e)}", chat_id=message.chat.id, message_id=status.message_id)

# --- DIRECT URL LINK ---
@bot.message_handler(func=lambda msg: True)
def handle_link_url(message):
    url = message.text.strip()
    if not url.startswith("http"):
        bot.reply_to(message, "Please send a valid HTTP/HTTPS link or a video file.")
        return

    status = bot.reply_to(message, "📥 Downloading video from link...")
    try:
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
        bot.edit_message_text(f"❌ Error: {str(e)}", chat_id=message.chat.id, message_id=status.message_id)

if __name__ == "__main__":
    # 1. Run web server in thread
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()

    # 2. Reset Telegram polling session to fix Error 409
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass

    print("Bot polling running smoothly...")
    bot.infinity_polling(skip_pending=True, timeout=20)
