import os
import gc
import uuid
import time
import email
import subprocess
import urllib.parse
import asyncio
import threading
import imageio_ffmpeg
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

# Get the exact standalone ffmpeg executable path
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

def add_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    logs_history.append(entry)
    if len(logs_history) > 60:
        logs_history.pop(0)

# ==============================================================================
# FORMATTING & VIDEO REMUX ENGINE
# ==============================================================================
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

def convert_mkv_to_mp4(input_path):
    """Converts/remuxes any MKV/video into standard fast-start web playable MP4"""
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_streamable.mp4"

    add_log(f"Remuxing video to web MP4 using FFmpeg: {input_path}")
    
    # 1. Fast stream copy with faststart (Instant remuxing for H.264/AAC)
    cmd = [
        FFMPEG_BIN, "-y", "-i", input_path,
        "-c:v", "copy", "-c:a", "copy",
        "-movflags", "+faststart",
        output_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            add_log("Fast remuxing to MP4 successful.")
            return output_path
    except Exception as e:
        add_log(f"Remux fast copy failed: {str(e)}")

    # 2. Fallback: Convert audio to AAC if MKV audio format is incompatible
    cmd_fallback = [
        FFMPEG_BIN, "-y", "-i", input_path,
        "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ]
    try:
        res = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            add_log("Audio-transcoded remux to MP4 successful.")
            return output_path
    except Exception as e:
        add_log(f"Remux fallback failed: {str(e)}")

    return input_path

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
    <title>Archive Manager & Direct Uploader</title>
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
            <h2 style="margin:0;">🚀 Internet Archive Hub (MKV -> MP4 Playable)</h2>
            <button class="btn" onclick="location.reload()">Refresh</button>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📤 Web Direct Upload</h3>
            <form method="POST" action="/upload" enctype="multipart/form-data" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                <input type="file" name="file" required style="color:#94a3b8;">
                <input type="text" name="custom_title" placeholder="Custom Title (Optional)" class="input-sm" style="flex:1;">
                <button type="submit" class="btn">Upload to Archive</button>
            </form>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📁 Manage Uploaded Files & Metadata</h3>
            <table>
                <thead>
                    <tr>
                        <th>Title / Filename</th>
                        <th>Size</th>
                        <th>Archive URL</th>
                        <th>Rename Title</th>
                    </tr>
                </thead>
                <tbody>
                    {file_rows}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📋 Real-time Logs</h3>
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
                    file_ext = os.path.splitext(file_name)[1] if file_name else ".mp4"
                    raw_path = f"/tmp/{item_id}{file_ext}"
                    clean_title = custom_title if custom_title else (os.path.splitext(file_name)[0] if file_name else f"Web Upload {item_id}")

                    with open(raw_path, "wb") as f:
                        f.write(uploaded_file_data)

                    final_path = convert_mkv_to_mp4(raw_path)
                    file_size_fmt = format_size(os.path.getsize(final_path))
                    add_log(f"Web Direct Upload processing: {clean_title} ({file_size_fmt})")

                    item = ia.get_item(item_id)
                    item.upload(
                        final_path,
                        metadata={
                            "title": clean_title,
                            "mediatype": "movies",
                            "collection": "opensource_movies"
                        },
                        access_key=IA_ACCESS,
                        secret_key=IA_SECRET
                    )

                    for p in [raw_path, final_path]:
                        if os.path.exists(p):
                            os.remove(p)

                    archive_url = f"https://archive.org/details/{item_id}"
                    uploaded_files_db.append({"id": item_id, "title": clean_title, "size": file_size_fmt, "url": archive_url})
                    add_log(f"Web Upload Complete: {archive_url}")

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
                    add_log(f"Metadata Updated: Renamed {item_id} -> '{new_title}'")
                except Exception as e:
                    add_log(f"Failed to rename metadata: {str(e)}")

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
bot = TelegramClient('tg_archive_uploader', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

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
    eta_seconds = (total - current) / speed if speed > 0 else 0
    eta_formatted = format_eta(eta_seconds)
    
    text = (
        f"📊 **{action_name}**\n\n"
        f"`[{bar}]` **{percentage:.1f}%**\n\n"
        f"⚡ **Speed:** `{format_size(speed)}/s`\n"
        f"📁 **Processed:** `{format_size(current)}` / `{format_size(total)}`\n"
        f"⏳ **ETA:** `{eta_formatted}`"
    )
    try:
        await status_msg.edit(text, parse_mode="markdown")
    except Exception:
        pass

# ==============================================================================
# TELEGRAM HANDLERS
# ==============================================================================
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    add_log(f"Chat {event.chat_id} issued /start")
    await event.reply(
        "👋 **Telegram Video & Movie Uploader Ready!**\n\n"
        "Forward any `.mkv` or `.mp4` video (up to 2GB) into this chat.\n"
        "• Automatically converts MKV to web-streamable MP4\n"
        "• Displays live ETA (hrs/mins/secs) and progress bar"
    )

@bot.on(events.NewMessage)
async def media_handler(event):
    if event.message.message and event.message.message.startswith("/"):
        return

    if event.message.media:
        file_name = None
        if hasattr(event.message.media, "document") and event.message.media.document:
            for attr in event.message.media.document.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    file_name = attr.file_name
                    break
        
        if not file_name:
            file_name = f"video_{uuid.uuid4().hex[:6]}.mkv"

        file_size = event.message.file.size if event.message.file else 0

        if file_size > 2000 * 1024 * 1024:
            add_log(f"File {file_name} ({format_size(file_size)}) exceeds Telegram bot limit of 2GB.")
            await event.reply(
                f"⚠️ **File is too large ({format_size(file_size)})!**\n\n"
                f"Telegram server restrictions disconnect bot tokens at **2.00 GB**.\n"
                f"Please upload a 720p/1080p file under 2.00 GB."
            )
            return

        add_log(f"Starting download: '{file_name}' ({format_size(file_size)})")
        status_msg = await event.reply("⏳ **Starting direct MTProto download...**")
        item_id = f"tg_{uuid.uuid4().hex[:8]}"
        raw_path = f"/tmp/{item_id}_{file_name}"

        try:
            start_time = time.time()
            last_update = [start_time]

            # Download media from Telegram
            await bot.download_media(
                event.message.media,
                file=raw_path,
                progress_callback=lambda c, t: update_progress(
                    c, t, status_msg, "Downloading File", start_time, last_update
                )
            )

            await status_msg.edit("⚙️ **Converting MKV to streamable MP4 for browser playback...**")
            
            # Execute conversion
            loop = asyncio.get_running_loop()
            final_path = await loop.run_in_executor(None, lambda: convert_mkv_to_mp4(raw_path))

            add_log(f"Uploading {final_path} to Archive.org...")
            await status_msg.edit("🚀 **Uploading to Internet Archive... Please wait.**")

            clean_title = os.path.splitext(file_name)[0]
            item = ia.get_item(item_id)
            
            # Upload streamable MP4 with movies mediatype
            await loop.run_in_executor(
                None,
                lambda: item.upload(
                    final_path,
                    metadata={
                        "title": clean_title,
                        "mediatype": "movies",
                        "collection": "opensource_movies"
                    },
                    access_key=IA_ACCESS,
                    secret_key=IA_SECRET
                )
            )

            archive_url = f"https://archive.org/details/{item_id}"
            final_size_fmt = format_size(os.path.getsize(final_path))
            uploaded_files_db.append({
                "id": item_id,
                "title": clean_title,
                "size": final_size_fmt,
                "url": archive_url
            })

            add_log(f"Upload complete: {archive_url}")
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"🎬 **File Name:** `{clean_title}.mp4`\n"
                f"📦 **Size:** `{final_size_fmt}`\n"
                f"▶️ **Play Online:** {archive_url}",
                link_preview=True
            )

        except Exception as e:
            add_log(f"Error handling media: {str(e)}")
            await status_msg.edit(f"❌ **Error:** `{str(e)}`")
        finally:
            for p in [raw_path, raw_path.replace(os.path.splitext(raw_path)[1], "_streamable.mp4")]:
                if os.path.exists(p):
                    os.remove(p)
            gc.collect()

# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    add_log(f"Telegram Media Uploader online with FFmpeg at {FFMPEG_BIN}")
    bot.run_until_disconnected()
        
