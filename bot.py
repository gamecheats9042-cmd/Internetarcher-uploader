import sys
import subprocess

# ==============================================================================
# AUTO-INSTALL DEPENDENCIES ON RUNTIME
# ==============================================================================
REQUIRED_PACKAGES = [
    "Telethon",
    "internetarchive",
    "requests",
    "cryptg",
    "boto3"
]

def ensure_dependencies():
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg.lower())
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"📦 Installing missing dependencies: {', '.join(missing)}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
            print("✅ Dependencies installed successfully!\n")
        except Exception as e:
            print(f"❌ Failed to auto-install dependencies: {e}")
            sys.exit(1)

ensure_dependencies()

# ==============================================================================
# CORE MODULE IMPORTS
# ==============================================================================
import os
import gc
import uuid
import time
import json
import sqlite3
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import boto3
from botocore.client import Config
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeFilename

# ==============================================================================
# CREDENTIALS & CONFIGURATION
# ==============================================================================
API_ID = int(os.getenv("TELEGRAM_API_ID", "2040"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8644006980:AAEKBACweZ9kg4M482anjYUkEP5O7DZF7wQ")

IA_ACCESS = os.getenv("IA_ACCESS_KEY", "SjzCWtMdMVYsRBXl")
IA_SECRET = os.getenv("IA_SECRET_KEY", "THTnm9iXNVafYy9b")

DB_FILE = "tasks.db"
DOWNLOAD_DIR = "/tmp/archive_hub"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB chunks
active_tasks = {}

logs_history = []
uploaded_files_db = []

def add_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    logs_history.append(entry)
    if len(logs_history) > 60:
        logs_history.pop(0)

# ==============================================================================
# DATABASE MANAGEMENT (Preserves Tasks Across Bot Restarts & Offline Events)
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS transfers (
            task_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            msg_id INTEGER,
            status_msg_id INTEGER,
            file_name TEXT,
            total_size INTEGER,
            downloaded_bytes INTEGER,
            uploaded_bytes INTEGER,
            stage TEXT,
            status TEXT,
            local_path TEXT,
            s3_upload_id TEXT,
            s3_parts_json TEXT
        )
    ''')
    conn.commit()
    conn.close()

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    res = c.fetchall()
    conn.close()
    return res

init_db()

# ==============================================================================
# FORMATTING UTILITIES & INLINE BUTTONS
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
    return f"{secs}s"

def make_keyboard(task_id, is_paused=False):
    if is_paused:
        return [
            [Button.inline("▶️ Resume", data=f"resume:{task_id}"),
             Button.inline("❌ Cancel", data=f"cancel:{task_id}")]
        ]
    return [
        [Button.inline("⏸ Pause", data=f"pause:{task_id}"),
         Button.inline("❌ Cancel", data=f"cancel:{task_id}")]
    ]

# ==============================================================================
# S3 CLIENT (Direct Multipart Support for Internet Archive)
# ==============================================================================
def get_ia_s3_client():
    return boto3.client(
        's3',
        endpoint_url='https://s3.us.archive.org',
        aws_access_key_id=IA_ACCESS,
        aws_secret_access_key=IA_SECRET,
        config=Config(signature_version='s3')
    )

# ==============================================================================
# TRANSFER ENGINE (Auto-Resume Telegram Download & S3 Upload)
# ==============================================================================
async def execute_transfer(task_id, bot_client):
    rows = db_execute("SELECT * FROM transfers WHERE task_id=?", (task_id,))
    if not rows:
        return
    
    r = rows[0]
    chat_id, msg_id, status_msg_id = r[1], r[2], r[3]
    file_name, total_size = r[4], r[5]
    stage, local_path = r[8], r[10]
    s3_upload_id, s3_parts_json = r[11], r[12]

    if task_id not in active_tasks:
        evt = asyncio.Event()
        evt.set()
        active_tasks[task_id] = {"pause": evt, "cancel": False}

    ctrl = active_tasks[task_id]

    async def send_progress(curr, tot, speed, eta, step_name):
        pct = (curr / tot * 100) if tot > 0 else 0
        filled = int(pct / 10)
        bar = "■" * filled + "□" * (10 - filled)
        status_text = "⏸ Paused" if not ctrl["pause"].is_set() else f"⚡ `{format_size(speed)}/s`"
        text = (
            f"🚀 **{step_name}**\n\n"
            f"`[{bar}]` **{pct:.1f}%**\n\n"
            f"**Status:** {status_text}\n"
            f"📁 **Processed:** `{format_size(curr)}` / `{format_size(tot)}`\n"
            f"⏳ **ETA:** `{format_eta(eta)}`"
        )
        try:
            await bot_client.edit_message(
                chat_id, status_msg_id, text,
                buttons=make_keyboard(task_id, is_paused=not ctrl["pause"].is_set())
            )
        except Exception:
            pass

    # STAGE 1: RESUMABLE TELEGRAM DOWNLOAD
    if stage == "downloading":
        original_msg = await bot_client.get_messages(chat_id, ids=msg_id)
        if not original_msg or not original_msg.media:
            add_log(f"Task {task_id} media missing.")
            return

        current_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        start_time = time.time()
        last_update = start_time
        downloaded_session = 0

        with open(local_path, "ab") as f:
            async for chunk in bot_client.iter_download(original_msg.media, offset=current_size, chunk_size=CHUNK_SIZE):
                while not ctrl["pause"].is_set():
                    await asyncio.sleep(0.5)

                if ctrl["cancel"]:
                    add_log(f"Task {task_id} cancelled during download.")
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))
                    await bot_client.edit_message(chat_id, status_msg_id, "❌ **Task Cancelled.**", buttons=None)
                    return

                f.write(chunk)
                current_size += len(chunk)
                downloaded_session += len(chunk)

                now = time.time()
                if (now - last_update >= 3) or (current_size >= total_size):
                    elapsed = now - start_time
                    speed = downloaded_session / elapsed if elapsed > 0 else 0
                    eta = (total_size - current_size) / speed if speed > 0 else 0
                    last_update = now

                    db_execute("UPDATE transfers SET downloaded_bytes=? WHERE task_id=?", (current_size, task_id))
                    await send_progress(current_size, total_size, speed, eta, "Downloading from Telegram")

        stage = "uploading"
        db_execute("UPDATE transfers SET stage='uploading' WHERE task_id=?", (task_id,))
        add_log(f"Download complete: {local_path}")

    # STAGE 2: RESUMABLE MULTIPART S3 UPLOAD (INTERNET ARCHIVE)
    if stage == "uploading":
        s3 = get_ia_s3_client()
        bucket = task_id
        key = file_name
        parts = json.loads(s3_parts_json) if s3_parts_json else []

        if not s3_upload_id:
            try:
                s3.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={'LocationConstraint': ''}
                )
            except Exception:
                pass

            mp = s3.create_multipart_upload(
                Bucket=bucket,
                Key=key,
                Metadata={
                    "mediatype": "movies",
                    "collection": "opensource_movies",
                    "title": os.path.splitext(file_name)[0]
                }
            )
            s3_upload_id = mp['UploadId']
            db_execute("UPDATE transfers SET s3_upload_id=? WHERE task_id=?", (s3_upload_id, task_id))

        total_parts = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        uploaded_part_nums = {p["PartNumber"] for p in parts}

        start_time = time.time()
        last_update = start_time
        uploaded_bytes = sum([CHUNK_SIZE for p in parts if p["PartNumber"] < total_parts])
        upload_session_bytes = 0

        with open(local_path, "rb") as f:
            for part_num in range(1, total_parts + 1):
                while not ctrl["pause"].is_set():
                    await asyncio.sleep(0.5)

                if ctrl["cancel"]:
                    add_log(f"Task {task_id} cancelled during upload.")
                    try:
                        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=s3_upload_id)
                    except Exception:
                        pass
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))
                    await bot_client.edit_message(chat_id, status_msg_id, "❌ **Task Cancelled.**", buttons=None)
                    return

                offset = (part_num - 1) * CHUNK_SIZE
                f.seek(offset)
                chunk_data = f.read(CHUNK_SIZE)

                if part_num in uploaded_part_nums:
                    continue

                res = s3.upload_part(
                    Bucket=bucket,
                    Key=key,
                    PartNumber=part_num,
                    UploadId=s3_upload_id,
                    Body=chunk_data
                )
                etag = res['ETag']
                parts.append({"PartNumber": part_num, "ETag": etag})
                uploaded_part_nums.add(part_num)

                uploaded_bytes += len(chunk_data)
                upload_session_bytes += len(chunk_data)

                now = time.time()
                elapsed = now - start_time
                speed = upload_session_bytes / elapsed if elapsed > 0 else 0
                eta = (total_size - uploaded_bytes) / speed if speed > 0 else 0

                db_execute(
                    "UPDATE transfers SET uploaded_bytes=?, s3_parts_json=? WHERE task_id=?",
                    (uploaded_bytes, json.dumps(parts), task_id)
                )

                if (now - last_update >= 3) or (uploaded_bytes >= total_size):
                    last_update = now
                    await send_progress(uploaded_bytes, total_size, speed, eta, "Uploading to Internet Archive")

        # Finalize multipart upload
        parts = sorted(parts, key=lambda p: p["PartNumber"])
        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=s3_upload_id,
            MultipartUpload={"Parts": parts}
        )

        archive_url = f"https://archive.org/details/{bucket}"
        uploaded_files_db.append({
            "id": bucket,
            "title": os.path.splitext(file_name)[0],
            "size": format_size(total_size),
            "url": archive_url
        })
        add_log(f"Upload complete: {archive_url}")

        if os.path.exists(local_path):
            os.remove(local_path)
        db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))

        await bot_client.edit_message(
            chat_id,
            status_msg_id,
            f"✅ **Upload Complete!**\n\n"
            f"🎬 **File Name:** `{file_name}`\n"
            f"📦 **Size:** `{format_size(total_size)}`\n"
            f"▶️ **Play Online:** {archive_url}",
            buttons=None,
            link_preview=True
        )
        gc.collect()

# ==============================================================================
# RESTART RECOVERY (Runs after offline or crash)
# ==============================================================================
async def resume_interrupted_tasks(bot_client):
    await asyncio.sleep(2)
    rows = db_execute("SELECT task_id FROM transfers")
    for r in rows:
        t_id = r[0]
        add_log(f"Restoring interrupted task: {t_id}")
        asyncio.create_task(execute_transfer(t_id, bot_client))

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
            </tr>
            """
        if not file_rows:
            file_rows = "<tr><td colspan='3' style='text-align:center; color:#94a3b8;'>No uploads yet.</td></tr>"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Archive Hub V2</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ background: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; margin: 0; }}
        .container {{ max-width: 950px; margin: 0 auto; }}
        .header {{ background: #1e293b; padding: 18px 24px; border-radius: 12px; display: flex; justify-content: space-between; align-items: center; border-left: 6px solid #10b981; margin-bottom: 20px; }}
        .card {{ background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
        .btn {{ background: #2563eb; color: white; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; }}
        .link-btn {{ color: #38bdf8; text-decoration: none; font-weight: 500; }}
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
            <h2 style="margin:0;">🚀 Internet Archive Hub V2 (Resumable)</h2>
            <button class="btn" onclick="location.reload()">Refresh</button>
        </div>
        <div class="card">
            <h3 style="margin-top:0;">📁 Uploaded Files</h3>
            <table>
                <thead>
                    <tr><th>Title</th><th>Size</th><th>Archive URL</th></tr>
                </thead>
                <tbody>{file_rows}</tbody>
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

    def log_message(self, format, *args):
        return

def run_web():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()

# ==============================================================================
# TELEGRAM BOT CLIENT & EVENT HANDLERS
# ==============================================================================
bot = TelegramClient('tg_archive_session', API_ID, API_HASH)

@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    await event.reply(
        "⚡ **Telegram to Internet Archive Hub V2 Ready!**\n\n"
        "• Send or forward any video or file (up to 2GB).\n"
        "• **Persistent Resume:** If the server restarts or goes offline, downloads and uploads continue from where they left off.\n"
        "• **Inline Controls:** Pause, Resume, or Cancel anytime using the interactive buttons."
    )

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode("utf-8")
    action, task_id = data.split(":")

    if task_id not in active_tasks:
        await event.answer("Task is no longer active.", alert=True)
        return

    ctrl = active_tasks[task_id]

    if action == "pause":
        ctrl["pause"].clear()
        await event.answer("Transfer paused.")
        await event.edit(buttons=make_keyboard(task_id, is_paused=True))
        add_log(f"Task {task_id} paused by user.")

    elif action == "resume":
        ctrl["pause"].set()
        await event.answer("Transfer resumed.")
        await event.edit(buttons=make_keyboard(task_id, is_paused=False))
        add_log(f"Task {task_id} resumed by user.")

    elif action == "cancel":
        ctrl["cancel"] = True
        ctrl["pause"].set()
        await event.answer("Cancelling task...")
        add_log(f"Task {task_id} cancelled by user.")

@bot.on(events.NewMessage)
async def media_handler(event):
    if event.message.message and event.message.message.startswith("/"):
        return

    if event.message.media:
        raw_name = None
        if hasattr(event.message.media, "document") and event.message.media.document:
            for attr in event.message.media.document.attributes:
                if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                    raw_name = attr.file_name
                    break
        
        if not raw_name:
            raw_name = f"video_{uuid.uuid4().hex[:6]}.mp4"

        file_size = event.message.file.size if event.message.file else 0

        if file_size > 2000 * 1024 * 1024:
            await event.reply("⚠️ File exceeds Telegram's 2.00 GB bot limit.")
            return

        task_id = f"tg_{uuid.uuid4().hex[:8]}"
        local_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_{raw_name}")

        status_msg = await event.reply(
            "⏳ **Initializing transfer...**",
            buttons=make_keyboard(task_id, is_paused=False)
        )

        db_execute(
            '''INSERT INTO transfers (
                task_id, chat_id, msg_id, status_msg_id, file_name, total_size,
                downloaded_bytes, uploaded_bytes, stage, status, local_path, s3_upload_id, s3_parts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (task_id, event.chat_id, event.message.id, status_msg.id, raw_name,
             file_size, 0, 0, "downloading", "active", local_path, "", json.dumps([]))
        )

        evt = asyncio.Event()
        evt.set()
        active_tasks[task_id] = {"pause": evt, "cancel": False}

        add_log(f"Queued task {task_id} for file '{raw_name}' ({format_size(file_size)})")
        asyncio.create_task(execute_transfer(task_id, bot))

# ==============================================================================
# MAIN ENTRY POINT (Clean Async Startup, No Deprecation Warnings)
# ==============================================================================
async def main():
    await bot.start(bot_token=BOT_TOKEN)
    asyncio.create_task(resume_interrupted_tasks(bot))
    await bot.run_until_disconnected()

if __name__ == "__main__":
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    add_log("Telegram Media Uploader V2 online with persistent DB & Controls.")
    asyncio.run(main())
