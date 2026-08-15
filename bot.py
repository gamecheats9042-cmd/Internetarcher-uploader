import os
import uuid
import time
import asyncio
import threading
import internetarchive as ia
from telethon import TelegramClient, events
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- CREDENTIALS ---
# Using official Telegram Web desktop client keys
API_ID = int(os.getenv("TELEGRAM_API_ID", "2040"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"

IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

# --- LIVE WEB LOG CONSOLE ---
logs_history = []

def add_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    logs_history.append(entry)
    if len(logs_history) > 60:
        logs_history.pop(0)

# --- WEB DASHBOARD (MANUAL REFRESH) ---
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        
        log_rows = "".join([f"<div class='log-row'>{log}</div>" for log in reversed(logs_history)]) or "<div class='log-row'>No activity recorded yet...</div>"
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Telegram Archive Uploader</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ background: #0b0f19; color: #e2e8f0; font-family: monospace; padding: 20px; margin: 0; }}
                .container {{ max-width: 850px; margin: 0 auto; }}
                .header {{ background: #1e293b; padding: 15px 20px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; border-left: 5px solid #10b981; }}
                .badge {{ background: #059669; color: white; padding: 6px 12px; border-radius: 20px; font-weight: bold; font-size: 0.8rem; }}
                .box {{ background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 20px; margin-top: 15px; }}
                .console {{ background: #030712; padding: 15px; border-radius: 8px; height: 350px; overflow-y: auto; color: #38bdf8; border: 1px solid #374151; font-size: 0.85rem; line-height: 1.6; }}
                .log-row {{ border-bottom: 1px solid #1f2937; padding: 4px 0; word-break: break-all; }}
                .btn {{ background: #3b82f6; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: bold; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>🎬 Telegram Media Uploader</h2>
                    <button class="btn" onclick="location.reload()">Refresh Logs</button>
                </div>
                <div class="box">
                    <h3>Live System & Upload Logs</h3>
                    <div class="console">{log_rows}</div>
                </div>
            </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        return

def run_web():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()

# --- INITIALIZE MTPROTO CLIENT ---
bot = TelegramClient('tg_archive_uploader', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- PROGRESS HELPER ---
def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"

async def update_progress(current, total, status_msg, action_name, start_time, last_update):
    now = time.time()
    if now - last_update[0] < 3 and current != total:
        return
    last_update[0] = now

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

# --- HANDLERS ---
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    add_log(f"Chat {event.chat_id} issued /start")
    await event.reply(
        "👋 **Telegram Video & Movie Uploader Ready!**\n\n"
        "Forward any video, `.mkv`, or `.mp4` file directly into this group or chat.\n\n"
        "• Handles large movie files up to 2GB/4GB\n"
        "• Displays live download speed, ETA, and progress bar\n"
        "• Uploads directly to the Internet Archive"
    )

@bot.on(events.NewMessage)
async def media_handler(event):
    if event.message.message and event.message.message.startswith("/"):
        return

    # Check for any media (videos, forwarded MKV files, documents)
    if event.message.media:
        # Extract filename and extension
        file_name = None
        if hasattr(event.message.media, "document") and event.message.media.document:
            for attr in event.message.media.document.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    file_name = attr.file_name
                    break
        
        if not file_name:
            file_name = f"video_{uuid.uuid4().hex[:6]}.mp4"

        file_size = event.message.file.size if event.message.file else 0
        add_log(f"Processing media: '{file_name}' ({format_size(file_size)}) in chat {event.chat_id}")

        status_msg = await event.reply("⏳ **Starting direct download...**")
        item_id = f"tg_{uuid.uuid4().hex[:8]}"
        file_path = f"/tmp/{item_id}_{file_name}"

        try:
            start_time = time.time()
            last_update = [start_time]

            # Direct MTProto download
            await bot.download_media(
                event.message.media,
                file=file_path,
                progress_callback=lambda c, t: update_progress(
                    c, t, status_msg, "Downloading File", start_time, last_update
                )
            )

            add_log(f"Download complete: {file_path}. Uploading to Archive.org...")
            await status_msg.edit("🚀 **Uploading to Internet Archive... Please wait.**")

            # Upload to Archive.org
            loop = asyncio.get_running_loop()
            item = ia.get_item(item_id)
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    file_path,
                    metadata={
                        "title": file_name,
                        "mediatype": "movies",
                        "collection": "opensource_movies"
                    },
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            add_log(f"Upload complete: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🎬 **File Name:** `{file_name}`\n"
                f"📦 **Size:** `{format_size(file_size)}`\n"
                f"🔗 **Archive Link:** {archive_url}",
                link_preview=True
            )

        except Exception as e:
            add_log(f"Error handling media: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

if __name__ == "__main__":
    # Start Web Server
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    add_log("Telegram Movie & Video Uploader online.")
    bot.run_until_disconnected()
