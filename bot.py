import os
import uuid
import threading
import requests
import telebot
import internetarchive as ia
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- CREDENTIALS ---
TELEGRAM_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"
IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# --- DUMMY HTTP SERVER FOR RENDER WEB SERVICE ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Internet Archive Telegram Bot</title>
            <style>
                body { font-family: sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                .card { background: #1e293b; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); text-align: center; max-width: 420px; width: 90%; }
                .status { display: inline-block; padding: 6px 14px; background: #10b981; color: white; border-radius: 20px; font-weight: bold; font-size: 0.9rem; margin-bottom: 1rem; }
                h1 { margin: 0 0 10px; font-size: 1.5rem; }
                p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="status">● BOT RUNNING ACTIVE</div>
                <h1>Internet Archive Uploader</h1>
                <p>Render Web Service is healthy and listening on port.</p>
                <p>Send video files or direct links to your Telegram bot to upload!</p>
            </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        # Silence HTTP access logs to keep terminal clean
        return

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    print(f"Web server started on port {port}")
    server.serve_forever()


# --- TELEGRAM BOT LOGIC ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 Bot is ready! Send me a direct video link or forward a video to upload directly to Internet Archive.",
    )

@bot.message_handler(content_types=["video", "document"])
def handle_video_file(message):
    try:
        status = bot.reply_to(message, "📥 Downloading video from Telegram...")

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
        status = bot.reply_to(message, "📥 Downloading video from link...")
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
    # Start the dummy web server on a background thread so Render detects open port
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    print("Bot polling started...")
    bot.infinity_polling()
