import os
import uuid
import time
import asyncio
import threading
import requests
import internetarchive as ia
from telethon import TelegramClient, events
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- CREDENTIALS ---
BOT_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"
IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

# Telethon API details (From my.telegram.org)
API_ID = int(os.getenv("TELEGRAM_API_ID", "12345678"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "your_api_hash_here")

# --- LIVE WEB LOGS STORAGE ---
logs_history = []

def add_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    logs_history.append(entry)
    if len(logs_history) > 60:
        logs_history.pop(0)

# --- WEB DASHBOARD & ERROR CONSOLE ---
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        
        log_rows = "".join([f"<div class='log-row'>{log}</div>" for log in reversed(logs_history)]) or "<div class='log-row'>No activity recorded yet...</div>"
        
        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <meta http-equiv="refresh" content="3">
            <title>Bot Monitor & Live Console</title>
            <style>
                body {{ background: #0b0f19; color: #e2e8f0; font-family: monospace; padding: 20px; margin: 0; }}
                .container {{ max-width: 900px; margin: 0 auto; }}
                .header {{ background: #1e293b; padding: 16px 24px; border-radius: 12px; display: flex; justify-content: space-between; align-items: center; border-left: 5px solid #10b981; }}
                .status {{ background: #059669; color: white; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 0.8rem; }}
                .box {{ background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 20px; margin-top: 15px; }}
                .console {{ background: #030712; padding: 15px; border-radius: 8px; height: 380px; overflow-y: auto; color: #38bdf8; border: 1px solid #374151; font-size: 0.85rem; line-height: 1.6; }}
                .log-row {{ border-bottom: 1px solid #1f2937; padding: 4px 0; word-break: break-all; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>🤖 Archive Uploader Console</h2>
                    <span class="status">● ONLINE (Auto-refresh 3s)</span>
                </div>
                <div class="box">
                    <h3>Real-time Activity & Error Logs</h3>
                    <div class="console">{log_rows}</div>
                </div>
            </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()

# --- INITIALIZE TELETHON CLIENT ---
bot = TelegramClient('archive_uploader_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- PROGRESS HELPER FUNCTION ---
def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"

async def progress_callback(current, total, status_msg, action_name, start_time, last_update_time):
    now = time.time()
    # Update Telegram every 3 seconds to avoid FloodWait limits
    if now - last_update_time[0] < 3 and current != total:
        return
    last_update_time[0] = now

    percentage = (current / total) * 100 if total > 0 else 0
    filled = int(percentage / 10)
    bar = "■" * filled + "□" * (10 - filled)
    
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    
    text = (
        f"📊 **{action_name}**\n\n"
        f"`[{bar}]` **{percentage:.1f}%**\n\n"
        f"⚡ **Speed:** `{format_size(speed)}/s`\n"
        f"📁 **Processed:** `{format_size(current)}` / `{format_size(total)}`\n"
        f"⏳ **ETA:** `{int(eta)}s`"
    )
    
    try:
        await status_msg.edit(text, parse_mode="markdown")
    except Exception:
        pass

# --- TELEGRAM BOT HANDLERS ---
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    add_log(f"Group/User {event.chat_id} issued /start")
    await event.reply(
        "👋 **Archive Uploader Bot Ready!**\n\n"
        "• Forward any large video (up to 2GB) into this group.\n"
        "• Send direct HTTP/HTTPS `.mp4` video links.\n"
        "• Live progress, speed, and ETA will be reported below."
    )

@bot.on(events.NewMessage)
async def handle_media_and_links(event):
    if event.message.message and event.message.message.startswith("/"):
        return

    # 1. Forwarded or Direct Media Files (Up to 2GB)
    if event.message.media and hasattr(event.message.media, "document"):
        file_size = event.message.file.size if event.message.file else 0
        add_log(f"Received media in chat {event.chat_id} (Size: {format_size(file_size)})")
        
        status_msg = await event.reply("⏳ **Initializing download...**")
        item_id = f"tg_{uuid.uuid4().hex[:8]}"
        file_path = f"/tmp/{item_id}.mp4"

        try:
            start_time = time.time()
            last_update = [start_time]

            # MTProto Fast Download with Live Progress
            await bot.download_media(
                event.message.media,
                file=file_path,
                progress_callback=lambda c, t: progress_callback(
                    c, t, status_msg, "Downloading from Telegram", start_time, last_update
                )
            )

            add_log(f"Download complete: {file_path}. Uploading to Internet Archive...")
            await status_msg.edit("🚀 **Uploading to Internet Archive... Please wait.**")

            # Run upload in background executor
            loop = asyncio.get_running_loop()
            item = ia.get_item(item_id)
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    file_path,
                    metadata={"title": f"Telegram Upload {item_id}", "mediatype": "movies"},
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            add_log(f"Upload success: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🔗 **Archive Link:** {archive_url}\n"
                f"📦 **Size:** `{format_size(file_size)}`",
                link_preview=True
            )

        except Exception as e:
            add_log(f"Error processing media: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
        return

    # 2. Direct Video URL Links
    text = (event.message.message or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        if "t.me/" in text:
            add_log(f"Rejected t.me redirect link in chat {event.chat_id}")
            await event.reply("❌ `t.me/...` links are Telegram web previews, not video files. Forward the video directly!")
            return

        add_log(f"Processing URL from chat {event.chat_id}: {text}")
        status_msg = await event.reply("⏳ **Connecting to URL...**")
        item_id = f"url_{uuid.uuid4().hex[:8]}"
        file_path = f"/tmp/{item_id}.mp4"

        try:
            start_time = time.time()
            last_update = [start_time]

            response = requests.get(text, stream=True, timeout=60)
            response.raise_for_status()
            total_length = int(response.headers.get('content-length', 0))
            downloaded = 0

            with open(file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_length > 0:
                            await progress_callback(
                                downloaded, total_length, status_msg, "Downloading URL Stream", start_time, last_update
                            )

            add_log(f"URL Download finished. Uploading {item_id} to Internet Archive...")
            await status_msg.edit("🚀 **Uploading to Internet Archive... Please wait.**")

            loop = asyncio.get_running_loop()
            item = ia.get_item(item_id)
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    file_path,
                    metadata={"title": f"URL Upload {item_id}", "mediatype": "movies"},
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            add_log(f"URL Upload complete: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🔗 **Archive Link:** {archive_url}",
                link_preview=True
            )

        except Exception as e:
            add_log(f"Error handling URL: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

if __name__ == "__main__":
    # Start Web Dashboard
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()

    add_log("Bot started successfully with MTProto 2GB support & Live Web Dashboard.")
    bot.run_until_disconnected()
