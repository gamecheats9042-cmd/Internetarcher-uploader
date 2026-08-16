import os
import gc
import io
import uuid
import time
import email
import urllib.parse
import asyncio
import threading
import requests
import internetarchive as ia
from telethon import TelegramClient, events
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==============================================================================
# CREDENTIALS & CONFIGURATION
# ==============================================================================
API_ID = int(os.getenv("TELEGRAM_API_ID", "2040"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ"

IA_ACCESS = "SjzCWtMdMVYsRBXl"
IA_SECRET = "THTnm9iXNVafYy9b"

logs_history = []
uploaded_files_db = []

def add_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    logs_history.append(entry)
    if len(logs_history) > 60:
        logs_history.pop(0)

def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"

def format_eta(seconds):
    if seconds <= 0:
        return "0s"
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    elif mins > 0:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"

# ==============================================================================
# WEB SERVER & MANAGEMENT DASHBOARD
# ==============================================================================
class DashboardHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_GET(self):
        log_rows = "".join([f"<div class='log-row'>{log}</div>" for log in reversed(logs_history)]) or "<div class='log-row'>No activity recorded yet...</div>"
        
        file_rows = ""
        for f in reversed(uploaded_files_db):
            file_rows += f"""
            <tr>
                <td><b>{f['title']}</b></td>
                <td>{f['size']}</td>
                <td><a href="{f['url']}" target="_blank" class="link-btn">▶️ Play Online</a></td>
                <td>
                    <form method="POST" action="/rename" style="display:inline-flex; gap: 5px;">
                        <input type="hidden" name="item_id" value="{f['id']}">
                        <input type="text" name="new_title" placeholder="New title..." required class="input-sm">
                        <button type="submit" class="btn-sm">Rename</button>
                    </form>
                </td>
            </tr>
            """
        if not file_rows:
            file_rows = "<tr><td colspan='4' style='text-align:center; color:#94a3b8;'>No uploads yet.</td></tr>"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Archive Direct Streamer</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ background: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; margin: 0; }}
        .container {{ max-width: 950px; margin: 0 auto; }}
        .header {{ background: #1e293b; padding: 18px 24px; border-radius: 12px; display: flex; justify-content: space-between; align-items: center; border-left: 6px solid #10b981; margin-bottom: 20px; }}
        .card {{ background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
        .btn {{ background: #2563eb; color: white; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; }}
        .btn-sm {{ background: #059669; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }}
        .input-sm {{ background: #1e293b; border: 1px solid #334155; color: white; padding: 6px 10px; border-radius: 6px; font-size: 0.8rem; }}
        .link-btn {{ color: #38bdf8; text-decoration: none; font-weight: 500; }}
        .link-btn:hover {{ text-decoration: underline; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #1f2937; font-size: 0.9rem; }}
        th {{ background: #1e293b; color: #94a3b8; font-weight: 600; }}
        .console {{ background: #030712; padding: 15px; border-radius: 8px; height: 180px; overflow-y: auto; color: #38bdf8; border: 1px solid #374151; font-family: monospace; font-size: 0.82rem; }}
        .log-row {{ border-bottom: 1px solid #1f2937; padding: 3px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin:0;">⚡ Zero-Disk Direct Stream Uploader</h2>
            <button class="btn" onclick="location.reload()">Refresh</button>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📤 Web Direct Upload</h3>
            <form method="POST" action="/upload" enctype="multipart/form-data" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                <input type="file" name="file" required style="color:#94a3b8;">
                <input type="text" name="custom_title" placeholder="Custom Title (Optional)" class="input-sm" style="flex:1;">
                <button type="submit" class="btn">Stream to Archive</button>
            </form>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📁 Manage Archive Files</h3>
            <table>
                <thead>
                    <tr>
                        <th>Title / Video</th>
                        <th>Size</th>
                        <th>Archive URL</th>
                        <th>Rename</th>
                    </tr>
                </thead>
                <tbody>
                    {file_rows}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📋 System Logs</h3>
            <div class="console">{log_rows}</div>
        </div>
    </div>
</body>
</html>"""
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path == "/upload":
            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", 0))
            
            if "multipart/form-data" in content_type and content_length > 0:
                body = self.rfile.read(content_length)
                msg_headers = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
                msg = email.message_from_bytes(msg_headers + body)
                
                uploaded_file_data = None
                custom_title = ""
                file_name = None

                for part in msg.walk():
                    disp = part.get("Content-Disposition", "")
                    if "name=\"file\"" in disp:
                        uploaded_file_data = part.get_payload(decode=True)
                        file_name = part.get_filename()
                    elif "name=\"custom_title\"" in disp:
                        custom_title = part.get_payload(decode=True).decode("utf-8", errors="ignore").strip()

                if uploaded_file_data:
                    item_id = f"web_{uuid.uuid4().hex[:8]}"
                    clean_name = os.path.splitext(file_name)[0] if file_name else f"Upload_{item_id}"
                    title = custom_title if custom_title else clean_name
                    
                    file_obj = io.BytesIO(uploaded_file_data)
                    file_size_fmt = format_size(len(uploaded_file_data))
                    add_log(f"Streaming web file directly to Archive: {title} ({file_size_fmt})")

                    item = ia.get_item(item_id)
                    item.upload(
                        {f"{clean_name}.mp4": file_obj},
                        metadata={
                            "title": title,
                            "mediatype": "movies",
                            "collection": "opensource_movies"
                        },
                        access_key=IA_ACCESS,
                        secret_key=IA_SECRET
                    )

                    archive_url = f"https://archive.org/details/{item_id}"
                    uploaded_files_db.append({"id": item_id, "title": title, "size": file_size_fmt, "url": archive_url})
                    add_log(f"Web Direct Upload complete: {archive_url}")

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self.path == "/rename":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len).decode("utf-8")
            params = urllib.parse.parse_qs(post_body)

            item_id = params.get("item_id", [""])[0]
            new_title = params.get("new_title", [""])[0]

            if item_id and new_title:
                try:
                    item = ia.get_item(item_id)
                    item.modify_metadata({"title": new_title}, access_key=IA_ACCESS, secret_key=IA_SECRET)
                    for f in uploaded_files_db:
                        if f["id"] == item_id:
                            f["title"] = new_title
                    add_log(f"Renamed {item_id} -> '{new_title}'")
                except Exception as e:
                    add_log(f"Rename failed: {str(e)}")

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

    def log_message(self, format, *args):
        return

def run_web():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()

# ==============================================================================
# TELETHON CLIENT INITIALIZATION
# ==============================================================================
bot = TelegramClient('direct_stream_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Progress tracking wrapper
class StreamProgressFile:
    def __init__(self, bot_client, message, total_size, status_msg, action_name):
        self.bot = bot_client
        self.message = message
        self.total = total_size
        self.current = 0
        self.status_msg = status_msg
        self.action_name = action_name
        self.start_time = time.time()
        self.last_update = self.start_time

    async def _update_ui(self):
        now = time.time()
        if now - self.last_update < 3 and self.current < self.total:
            return
        self.last_update = now

        percentage = (self.current / self.total) * 100 if self.total > 0 else 0
        filled = int(percentage / 10)
        bar = "■" * filled + "□" * (10 - filled)
        
        elapsed = now - self.start_time
        speed = self.current / elapsed if elapsed > 0 else 0
        eta_seconds = (self.total - self.current) / speed if speed > 0 else 0
        
        text = (
            f"🚀 **{self.action_name}**\n\n"
            f"`[{bar}]` **{percentage:.1f}%**\n\n"
            f"⚡ **Speed:** `{format_size(speed)}/s`\n"
            f"📁 **Streamed:** `{format_size(self.current)}` / `{format_size(self.total)}`\n"
            f"⏳ **ETA:** `{format_eta(eta_seconds)}`"
        )
        try:
            await self.status_msg.edit(text, parse_mode="markdown")
        except Exception:
            pass

    async def stream_generator(self):
        async for chunk in self.bot.iter_download(self.message.media, chunk_size=1024 * 1024):
            self.current += len(chunk)
            await self._update_ui()
            yield chunk

# Synchronous adapter for S3 upload streaming
class SyncStreamIterator:
    def __init__(self, async_gen, loop):
        self.async_gen = async_gen
        self.loop = loop

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return asyncio.run_coroutine_threadsafe(self.async_gen.__anext__(), self.loop).result()
        except StopAsyncIteration:
            raise StopIteration

# ==============================================================================
# TELEGRAM HANDLERS
# ==============================================================================
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    add_log(f"Chat {event.chat_id} issued /start")
    await event.reply(
        "⚡ **Direct Telegram to Internet Archive Streamer Ready!**\n\n"
        "Forward any video or `.mkv` file (up to 2GB) into this chat.\n"
        "• Streams directly without storing or filling Render disk space\n"
        "• Live real-time speed, ETA, and progress bar\n"
        "• Automatic web streaming playback on Archive.org"
    )

@bot.on(events.NewMessage)
async def media_handler(event):
    if event.message.message and event.message.message.startswith("/"):
        return

    if event.message.media:
        raw_name = None
        if hasattr(event.message.media, "document") and event.message.media.document:
            for attr in event.message.media.document.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    raw_name = attr.file_name
                    break
        
        if not raw_name:
            raw_name = f"video_{uuid.uuid4().hex[:6]}.mkv"

        clean_base = os.path.splitext(raw_name)[0]
        target_filename = f"{clean_base}.mp4"
        file_size = event.message.file.size if event.message.file else 0

        if file_size > 2000 * 1024 * 1024:
            add_log(f"File {raw_name} ({format_size(file_size)}) exceeds Telegram bot limit of 2GB.")
            await event.reply(
                f"⚠️ **File is too large ({format_size(file_size)})!**\n\n"
                f"Telegram server restrictions disconnect bot tokens at **2.00 GB**.\n"
                f"Please upload a 720p or 1080p version under 2.00 GB."
            )
            return

        add_log(f"Direct stream pipe started: '{target_filename}' ({format_size(file_size)})")
        status_msg = await event.reply("⚡ **Establishing direct pipe to Internet Archive...**")
        item_id = f"tg_{uuid.uuid4().hex[:8]}"

        try:
            loop = asyncio.get_running_loop()
            progress_tracker = StreamProgressFile(bot, event.message, file_size, status_msg, "Streaming to Archive.org")
            sync_stream = SyncStreamIterator(progress_tracker.stream_generator(), loop)

            item = ia.get_item(item_id)

            # Direct cloud upload without disk caching
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    {target_filename: sync_stream},
                    metadata={
                        "title": clean_base,
                        "mediatype": "movies",
                        "collection": "opensource_movies",
                        "format": "h.264"
                    },
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            uploaded_files_db.append({
                "id": item_id,
                "title": clean_base,
                "size": format_size(file_size),
                "url": archive_url
            })

            add_log(f"Direct stream complete: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🎬 **File Name:** `{target_filename}`\n"
                f"📦 **Size:** `{format_size(file_size)}`\n"
                f"▶️ **Play Online:** {archive_url}",
                link_preview=True
            )

        except Exception as e:
            add_log(f"Stream error: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            gc.collect()

# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    add_log("Zero-Disk Direct Stream Bot online.")
    bot.run_until_disconnected()
