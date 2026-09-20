import sys
import subprocess

# ==============================================================================
# AUTO-INSTALL DEPENDENCIES ON RUNTIME
# ==============================================================================
REQUIRED_PACKAGES = [
    "Telethon",
    "internetarchive",
    "requests",
    "cryptg"
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
import re
import sqlite3
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import internetarchive as ia
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
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
DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CHUNK_SIZE = 1024 * 1024  # 1 MB chunk
MAX_CONCURRENT_TRANSFERS = 1

queue_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSFERS)
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
# DATABASE MANAGEMENT
# ==============================================================================
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = get_db()
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
            created_at REAL
        )
    ''')
    conn.commit()
    conn.close()

def db_execute(query, params=()):
    conn = get_db()
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

def clean_archive_name(name):
    base, ext = os.path.splitext(name)
    clean_base = re.sub(r'[^a-zA-Z0-9]', '_', base)
    clean_base = re.sub(r'_+', '_', clean_base).strip('_')
    clean_ext = re.sub(r'[^a-zA-Z0-9.]', '', ext).lower()
    return f"{clean_base}{clean_ext}"

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
# SAFE TELEGRAM MESSAGE UPDATER
# ==============================================================================
last_telegram_edit_time = {}

async def safe_edit_message(bot_client, chat_id, message_id, text, buttons=None, force=False):
    now = time.time()
    last_time = last_telegram_edit_time.get(message_id, 0)
    if not force and (now - last_time < 5.0):
        return
    last_telegram_edit_time[message_id] = now

    try:
        await bot_client.edit_message(chat_id, message_id, text, buttons=buttons)
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        try:
            await bot_client.edit_message(chat_id, message_id, text, buttons=buttons)
        except Exception:
            pass
    except Exception:
        pass

# ==============================================================================
# NETWORK STREAMING CLASS (Fixes 411 Length Required & Tracks Real Network Speed)
# ==============================================================================
class RealNetworkUploadStream:
    def __init__(self, filepath, total_size, task_id, loop, progress_callback):
        self._file = open(filepath, 'rb')
        self.total_size = total_size
        self.bytes_sent = 0
        self.task_id = task_id
        self.loop = loop
        self.progress_callback = progress_callback
        self.start_time = time.time()
        self.last_update = self.start_time

    def __len__(self):
        return self.total_size

    def read(self, size=-1):
        if self.task_id in active_tasks:
            ctrl = active_tasks[self.task_id]
            if ctrl.get("cancel"):
                raise Exception("TRANSFER_CANCELLED")
            while not ctrl["pause"].is_set():
                time.sleep(0.5)
                if ctrl.get("cancel"):
                    raise Exception("TRANSFER_CANCELLED")

        chunk = self._file.read(size)
        if chunk:
            self.bytes_sent += len(chunk)
            now = time.time()
            if (now - self.last_update >= 5.0) or (self.bytes_sent >= self.total_size):
                self.last_update = now
                elapsed = now - self.start_time
                speed = self.bytes_sent / elapsed if elapsed > 0 else 0
                eta = (self.total_size - self.bytes_sent) / speed if speed > 0 else 0

                db_execute("UPDATE transfers SET uploaded_bytes=? WHERE task_id=?", (self.bytes_sent, self.task_id))
                asyncio.run_coroutine_threadsafe(
                    self.progress_callback(self.bytes_sent, self.total_size, speed, eta),
                    self.loop
                )
        return chunk

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def tell(self):
        return self._file.tell()

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

# ==============================================================================
# PIPELINE: TELEGRAM DOWNLOAD & RESUMABLE DIRECT S3 UPLOAD
# ==============================================================================
async def execute_transfer(task_id, bot_client):
    rows = db_execute("SELECT * FROM transfers WHERE task_id=?", (task_id,))
    if not rows:
        return

    r = rows[0]
    chat_id, msg_id, status_msg_id = r[1], r[2], r[3]
    file_name, total_size = r[4], r[5]
    stage, local_path = r[8], r[10]

    if task_id not in active_tasks:
        evt = asyncio.Event()
        evt.set()
        active_tasks[task_id] = {"pause": evt, "cancel": False}

    ctrl = active_tasks[task_id]

    async with queue_semaphore:
        async def send_download_progress(curr, tot, speed, eta):
            pct = min(100.0, (curr / tot * 100) if tot > 0 else 0)
            filled = int(pct / 10)
            bar = "■" * filled + "□" * (10 - filled)
            status_text = "⏸ Paused" if not ctrl["pause"].is_set() else f"⚡ `{format_size(speed)}/s`"
            text = (
                f"📥 **Downloading from Telegram**\n\n"
                f"`[{bar}]` **{pct:.1f}%**\n\n"
                f"**Status:** {status_text}\n"
                f"📁 **Downloaded:** `{format_size(curr)}` / `{format_size(tot)}`\n"
                f"⏳ **ETA:** `{format_eta(eta)}`"
            )
            await safe_edit_message(
                bot_client, chat_id, status_msg_id, text,
                buttons=make_keyboard(task_id, is_paused=not ctrl["pause"].is_set())
            )

        async def send_upload_progress(curr, tot, speed, eta):
            pct = min(100.0, (curr / tot * 100) if tot > 0 else 0)
            filled = int(pct / 10)
            bar = "■" * filled + "□" * (10 - filled)
            status_text = "⏸ Paused" if not ctrl["pause"].is_set() else f"⚡ `{format_size(speed)}/s`"
            text = (
                f"🚀 **Uploading to Internet Archive**\n\n"
                f"`[{bar}]` **{pct:.1f}%**\n\n"
                f"**Status:** {status_text}\n"
                f"📁 **Uploaded:** `{format_size(curr)}` / `{format_size(tot)}`\n"
                f"⏳ **ETA:** `{format_eta(eta)}`"
            )
            await safe_edit_message(
                bot_client, chat_id, status_msg_id, text,
                buttons=make_keyboard(task_id, is_paused=not ctrl["pause"].is_set())
            )

        try:
            # ------------------------------------------------------------------
            # STAGE 1: TELEGRAM RESUMABLE DOWNLOAD
            # ------------------------------------------------------------------
            if stage == "downloading":
                original_msg = await bot_client.get_messages(chat_id, ids=msg_id)
                if not original_msg or not original_msg.media:
                    add_log(f"Task {task_id} media missing.")
                    return

                current_size = 0
                if os.path.exists(local_path):
                    raw_disk = os.path.getsize(local_path)
                    aligned_size = (raw_disk // CHUNK_SIZE) * CHUNK_SIZE
                    if aligned_size != raw_disk:
                        with open(local_path, "r+b") as f:
                            f.truncate(aligned_size)
                    current_size = aligned_size
                    add_log(f"Resuming download {task_id} at {format_size(current_size)}")

                start_time = time.time()
                downloaded_session = 0

                with open(local_path, "ab") as f:
                    async for chunk in bot_client.iter_download(original_msg.media, offset=current_size, chunk_size=CHUNK_SIZE):
                        while not ctrl["pause"].is_set():
                            await asyncio.sleep(0.5)

                        if ctrl["cancel"]:
                            raise Exception("TRANSFER_CANCELLED")

                        f.write(chunk)
                        current_size += len(chunk)
                        downloaded_session += len(chunk)

                        now = time.time()
                        elapsed = now - start_time
                        speed = downloaded_session / elapsed if elapsed > 0 else 0
                        eta = (total_size - current_size) / speed if speed > 0 else 0

                        db_execute("UPDATE transfers SET downloaded_bytes=? WHERE task_id=?", (current_size, task_id))
                        await send_download_progress(current_size, total_size, speed, eta)

                stage = "uploading"
                db_execute("UPDATE transfers SET stage='uploading' WHERE task_id=?", (task_id,))
                add_log(f"Download complete: {local_path}")

            # ------------------------------------------------------------------
            # STAGE 2: DIRECT S3 STREAMING UPLOAD
            # ------------------------------------------------------------------
            if stage == "uploading":
                if not os.path.exists(local_path):
                    add_log(f"Local file missing for {task_id}. Re-queuing download.")
                    db_execute("UPDATE transfers SET stage='downloading', downloaded_bytes=0 WHERE task_id=?", (task_id,))
                    asyncio.create_task(execute_transfer(task_id, bot_client))
                    return

                actual_size = os.path.getsize(local_path)
                clean_title = os.path.splitext(file_name)[0]
                safe_remote_name = clean_archive_name(file_name)

                archive_item_id = f"tg_{uuid.uuid4().hex[:10]}"
                add_log(f"Uploading '{safe_remote_name}' ({format_size(actual_size)}) to item {archive_item_id}...")

                await safe_edit_message(
                    bot_client, chat_id, status_msg_id,
                    "🚀 **Connecting & Initializing upload to Internet Archive...**",
                    buttons=make_keyboard(task_id, is_paused=False),
                    force=True
                )

                upload_url = f"https://s3.us.archive.org/{archive_item_id}/{safe_remote_name}"
                headers = {
                    "authorization": f"LOW {IA_ACCESS}:{IA_SECRET}",
                    "x-archive-auto-make-bucket": "1",
                    "x-archive-meta-mediatype": "movies",
                    "x-archive-meta-collection": "opensource_movies",
                    "x-archive-meta-title": clean_title,
                    "Content-Length": str(actual_size)
                }

                loop = asyncio.get_running_loop()

                def do_s3_upload():
                    with RealNetworkUploadStream(local_path, actual_size, task_id, loop, send_upload_progress) as stream:
                        session = requests.Session()
                        response = session.put(
                            upload_url,
                            data=stream,
                            headers=headers,
                            timeout=(30, 3600)
                        )
                        if response.status_code not in [200, 201]:
                            raise Exception(f"Archive.org returned status {response.status_code}: {response.text}")

                await loop.run_in_executor(None, do_s3_upload)

                archive_url = f"https://archive.org/details/{archive_item_id}"
                uploaded_files_db.append({
                    "id": archive_item_id,
                    "title": clean_title,
                    "size": format_size(actual_size),
                    "url": archive_url
                })
                add_log(f"Upload complete: {archive_url}")

                if os.path.exists(local_path):
                    os.remove(local_path)
                db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))

                await safe_edit_message(
                    bot_client, chat_id, status_msg_id,
                    f"✅ **Upload Complete!**\n\n"
                    f"🎬 **File Name:** `{file_name}`\n"
                    f"📦 **Size:** `{format_size(actual_size)}`\n"
                    f"▶️ **Play Online:** {archive_url}",
                    buttons=None,
                    force=True
                )

        except Exception as e:
            if "TRANSFER_CANCELLED" in str(e) or ctrl.get("cancel"):
                add_log(f"Task {task_id} cancelled.")
                if os.path.exists(local_path):
                    os.remove(local_path)
                db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))
                await safe_edit_message(bot_client, chat_id, status_msg_id, "❌ **Task Cancelled.**", buttons=None, force=True)
            else:
                add_log(f"Transfer error on {task_id}: {str(e)}")
                await safe_edit_message(bot_client, chat_id, status_msg_id, f"❌ **Error:** `{str(e)}`", buttons=None, force=True)
        finally:
            gc.collect()

# ==============================================================================
# RESTART RECOVERY WORKER
# ==============================================================================
async def resume_interrupted_tasks(bot_client):
    await asyncio.sleep(4)
    rows = db_execute("SELECT task_id FROM transfers ORDER BY created_at ASC")
    for r in rows:
        t_id = r[0]
        add_log(f"Restoring saved task: {t_id}")
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
    <title>Archive Hub V3</title>
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
            <h2 style="margin:0;">🚀 Internet Archive Hub V3</h2>
            <button class="btn" onclick="location.reload()">Refresh</button>
        </div>
        <div class="card">
            <h3 style="margin-top:0;">📁 Uploaded Files</h3>
            <table>
                <thead><tr><th>Title</th><th>Size</th><th>Archive URL</th></tr></thead>
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
        "⚡ **Telegram to Internet Archive Hub Ready!**\n\n"
        "• Send or forward any video (up to 2GB).\n"
        "• Real-time accurate upload progress bar.\n"
        "• Pause, Resume, or Cancel anytime with the buttons below."
    )

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode("utf-8")
    action, task_id = data.split(":")

    if task_id not in active_tasks:
        await event.answer("Task is no longer active or in queue.", alert=True)
        return

    ctrl = active_tasks[task_id]

    if action == "pause":
        ctrl["pause"].clear()
        await event.answer("Transfer paused.")
        await safe_edit_message(bot, event.chat_id, event.message_id, buttons=make_keyboard(task_id, is_paused=True), force=True)
        add_log(f"Task {task_id} paused by user.")

    elif action == "resume":
        ctrl["pause"].set()
        await event.answer("Transfer resumed.")
        await safe_edit_message(bot, event.chat_id, event.message_id, buttons=make_keyboard(task_id, is_paused=False), force=True)
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
            "⏳ **Queued for transfer...**",
            buttons=make_keyboard(task_id, is_paused=False)
        )

        db_execute(
            '''INSERT INTO transfers (
                task_id, chat_id, msg_id, status_msg_id, file_name, total_size,
                downloaded_bytes, uploaded_bytes, stage, status, local_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (task_id, event.chat_id, event.message.id, status_msg.id, raw_name,
             file_size, 0, 0, "downloading", "active", local_path, time.time())
        )

        evt = asyncio.Event()
        evt.set()
        active_tasks[task_id] = {"pause": evt, "cancel": False}

        add_log(f"Queued file: '{raw_name}' ({format_size(file_size)})")
        asyncio.create_task(execute_transfer(task_id, bot))

# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
async def main():
    await bot.start(bot_token=BOT_TOKEN)
    asyncio.create_task(resume_interrupted_tasks(bot))
    await bot.run_until_disconnected()

if __name__ == "__main__":
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    add_log("Telegram Media Uploader online (Fixed 411 & True S3 Streaming).")
    asyncio.run(main())
