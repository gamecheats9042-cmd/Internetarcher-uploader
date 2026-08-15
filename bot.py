import os
import re
import uuid
import time
import glob
import asyncio
import threading
import requests
import yt_dlp
import internetarchive as ia
from telethon import TelegramClient, events
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==========================================
# CREDENTIALS
# ==========================================
API_ID = int(os.getenv("TELEGRAM_API_ID", "2040"))  # Official Telegram Web client ID
API_HASH = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")  # Official Web hash
BOT_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"

IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

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
        <html>
        <head>
            <title>All-in-One Archive Uploader</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta http-equiv="refresh" content="3">
            <style>
                body {{ background: #0b0f19; color: #e2e8f0; font-family: monospace; padding: 20px; margin: 0; }}
                .container {{ max-width: 900px; margin: 0 auto; }}
                .header {{ background: #1e293b; padding: 15px 20px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; border-left: 5px solid #10b981; }}
                .badge {{ background: #059669; color: white; padding: 6px 12px; border-radius: 20px; font-weight: bold; font-size: 0.8rem; }}
                .box {{ background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 20px; margin-top: 15px; }}
                .console {{ background: #030712; padding: 15px; border-radius: 8px; height: 350px; overflow-y: auto; color: #38bdf8; border: 1px solid #374151; font-size: 0.85rem; line-height: 1.6; }}
                .log-row {{ border-bottom: 1px solid #1f2937; padding: 4px 0; word-break: break-all; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>⚡ All-in-One Archive Uploader</h2>
                    <span class="badge">● ONLINE (Auto-refresh 3s)</span>
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
bot = TelegramClient('all_in_one_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

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
        "👋 **All-in-One Internet Archive Bot is Active!**\n\n"
        "⚡ **Supported:**\n"
        "• **Forward large Telegram videos** directly (No 20MB limit)\n"
        "• **YouTube & Social Media Links**\n"
        "• **Direct Video/File URLs**\n\n"
        "Live progress bar, speed, and real-time logs enabled."
    )

@bot.on(events.NewMessage)
async def main_handler(event):
    if event.message.message and event.message.message.startswith("/"):
        return

    # 1. DIRECT FORWARDED / UPLOADED TELEGRAM MEDIA (NO 20MB LIMIT)
    if event.message.media and hasattr(event.message.media, "document"):
        file_size = event.message.file.size if event.message.file else 0
        file_name = event.message.file.name or f"video_{uuid.uuid4().hex[:6]}.mp4"
        add_log(f"Received media '{file_name}' ({format_size(file_size)}) in chat {event.chat_id}")
        
        status_msg = await event.reply("⏳ **Initializing direct MTProto download...**")
        item_id = f"tg_{uuid.uuid4().hex[:8]}"
        file_path = f"/tmp/{item_id}_{file_name}"

        try:
            start_time = time.time()
            last_update = [start_time]

            # MTProto direct streaming download
            await bot.download_media(
                event.message.media,
                file=file_path,
                progress_callback=lambda c, t: update_progress(
                    c, t, status_msg, "Downloading Telegram File", start_time, last_update
                )
            )

            add_log(f"Download complete: {file_path}. Uploading to Internet Archive...")
            await status_msg.edit("🚀 **Uploading to Internet Archive... Please wait.**")

            loop = asyncio.get_running_loop()
            item = ia.get_item(item_id)
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    file_path,
                    metadata={"title": file_name, "mediatype": "movies"},
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            add_log(f"Upload complete: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🎬 **File:** `{file_name}`\n"
                f"📦 **Size:** `{format_size(file_size)}`\n"
                f"🔗 **Archive Link:** {archive_url}",
                link_preview=True
            )

        except Exception as e:
            add_log(f"Error handling Telegram media: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
        return

    # 2. YOUTUBE, DIRECT URLS, OR STREAMING LINKS
    text = (event.message.message or "").strip()
    url_match = re.search(r'(https?://[^\s]+)', text)
    
    if url_match:
        url = url_match.group(0)
        add_log(f"Processing URL from chat {event.chat_id}: {url}")
        status_msg = await event.reply("⚡ **Analyzing link & fetching media...**")
        
        item_id = f"url_{uuid.uuid4().hex[:8]}"
        output_template = f"/tmp/{item_id}.%(ext)s"
        last_update = [time.time()]

        def ytdl_hook(d):
            if d['status'] == 'downloading':
                now = time.time()
                if now - last_update[0] >= 3:
                    last_update[0] = now
                    downloaded = d.get('downloaded_bytes', 0)
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    speed = d.get('speed') or 0
                    eta = d.get('eta') or 0

                    if total > 0:
                        pct = (downloaded / total) * 100
                        filled = int(pct / 10)
                        bar = "■" * filled + "□" * (10 - filled)
                        text_prog = (
                            f"📥 **Downloading Stream...**\n\n"
                            f"`[{bar}]` **{pct:.1f}%**\n\n"
                            f"⚡ **Speed:** `{format_size(speed)}/s`\n"
                            f"📁 **Size:** `{format_size(downloaded)}` / `{format_size(total)}`\n"
                            f"⏳ **ETA:** `{eta}s`"
                        )
                    else:
                        text_prog = f"📥 **Downloading Stream...**\n\n⚡ **Speed:** `{format_size(speed)}/s`\n📁 **Downloaded:** `{format_size(downloaded)}`"

                    asyncio.create_task(status_msg.edit(text_prog, parse_mode="markdown"))

        ydl_opts = {
            'outtmpl': output_template,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'progress_hooks': [ytdl_hook],
            'quiet': True,
            'no_warnings': True
        }

        try:
            title = f"Upload {item_id}"
            try:
                loop = asyncio.get_running_loop()
                def extract():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(url, download=True)
                info = await loop.run_in_executor(None, extract)
                title = info.get('title', title)
            except Exception:
                # Binary direct stream fallback
                file_path = f"/tmp/{item_id}.mp4"
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(file_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)

            matching_files = glob.glob(f"/tmp/{item_id}.*")
            if not matching_files:
                raise Exception("Could not download the file.")

            downloaded_file = matching_files[0]
            fsize = os.path.getsize(downloaded_file)

            add_log(f"URL Download complete ({format_size(fsize)}). Uploading to Internet Archive...")
            await status_msg.edit("🚀 **Uploading to Internet Archive... Please wait.**")

            loop = asyncio.get_running_loop()
            item = ia.get_item(item_id)
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    downloaded_file,
                    metadata={"title": title, "mediatype": "movies"},
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            add_log(f"Upload Complete: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🎬 **Title:** `{title}`\n"
                f"📦 **Size:** `{format_size(fsize)}`\n"
                f"🔗 **Archive Link:** {archive_url}",
                link_preview=True
            )

        except Exception as e:
            add_log(f"URL Process Error: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            for f in glob.glob(f"/tmp/{item_id}.*"):
                if os.path.exists(f):
                    os.remove(f)

if __name__ == "__main__":
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    add_log("All-in-One Bot online with MTProto + yt-dlp support.")
    bot.run_until_disconnected()
