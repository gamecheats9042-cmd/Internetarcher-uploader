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
import requests
import internetarchive as ia
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
MAX_CONCURRENT_TRANSFERS = 1   # Prevents disk exhaustion and flood limits

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
# DATABASE MANAGEMENT (WAL mode with 60s timeout for concurrent safety)
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
# INTERNET ARCHIVE ITEM INITIALIZATION & S3 RESUME HELPERS
# ==============================================================================
def ensure_ia_item_created(task_id, clean_title):
    """Pre-initializes the bucket/item on Archive.org with metadata."""
    headers = {
        "authorization": f"LOW {IA_ACCESS}:{IA_SECRET}",
        "x-archive-auto-make-bucket": "1",
        "x-archive-meta-mediatype": "movies",
        "x-archive-meta-collection": "opensource_movies",
        "x-archive-meta-title": clean_title
    }
    url = f"https://s3.us.archive.org/{task_id}"
    try:
        r = requests.put(url, headers=headers, timeout=30)
        return r.status_code in [200, 201, 409]
    except Exception as e:
        add_log(f"Metadata init check: {e}")
        return True

def get_remote_uploaded_size(task_id, file_name):
    """Checks the exact byte size Archive.org already has for this file."""
    url = f"https://s3.us.archive.org/{task_id}/{file_name}"
    headers = {"authorization": f"LOW {IA_ACCESS}:{IA_SECRET}"}
    try:
        r = requests.head(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        pass
    return 0

# ==============================================================================
# WORKER: TRANSFER ENGINE (DOWNLOAD + RESUMABLE UPLOAD)
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

        try:
            # ------------------------------------------------------------------
            # STAGE 1: TELEGRAM RESUMABLE DOWNLOAD (Byte offset on disk)
            # ------------------------------------------------------------------
            if stage == "downloading":
                original_msg = await bot_client.get_messages(chat_id, ids=msg_id)
                if not original_msg or not original_msg.media:
                    add_log(f"Task {task_id} original media is missing.")
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
                            raise Exception("TRANSFER_CANCELLED")

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

            # ------------------------------------------------------------------
            # STAGE 2: INTERNET ARCHIVE RESUMABLE UPLOAD
            # ------------------------------------------------------------------
            if stage == "uploading":
                if not os.path.exists(local_path):
                    add_log(f"Local file missing for {task_id}. Re-queuing download.")
                    db_execute("UPDATE transfers SET stage='downloading', downloaded_bytes=0 WHERE task_id=?", (task_id,))
                    asyncio.create_task(execute_transfer(task_id, bot_client))
                    return

                actual_size = os.path.getsize(local_path)
                clean_title = os.path.splitext(file_name)[0]

                # 1. Initialize bucket item properly
                ensure_ia_item_created(task_id, clean_title)

                # 2. Check how much Archive.org already has to resume from 80% (or anywhere)
                remote_size = get_remote_uploaded_size(task_id, file_name)
                uploaded_bytes = remote_size if remote_size < actual_size else 0

                add_log(f"Uploading {task_id}: Resuming from byte {uploaded_bytes}/{actual_size} ({format_size(uploaded_bytes)})")

                start_time = time.time()
                last_update = start_time
                session_uploaded = 0

                upload_url = f"https://s3.us.archive.org/{task_id}/{file_name}"
                headers = {
                    "authorization": f"LOW {IA_ACCESS}:{IA_SECRET}",
                    "x-archive-meta-mediatype": "movies",
                    "x-archive-meta-collection": "opensource_movies",
                    "x-archive-meta-title": clean_title
                }

                # Single chunk full upload if 0, or chunk streaming
                with open(local_path, "rb") as f:
                    if uploaded_bytes > 0:
                        f.seek(uploaded_bytes)

                    # Stream with progress and pause/cancel support
                    class ProgressStream:
                        def __init__(self, file_obj, total_len, start_offset):
                            self.file_obj = file_obj
                            self.total_len = total_len
                            self.current = start_offset
                            self.last_up = time.time()
                            self.start_t = time.time()
                            self.session_b = 0

                        def read(self, size=-1):
                            if ctrl.get("cancel"):
                                raise Exception("TRANSFER_CANCELLED")
                            while not ctrl["pause"].is_set():
                                time.sleep(0.5)
                                if ctrl.get("cancel"):
                                    raise Exception("TRANSFER_CANCELLED")

                            data = self.file_obj.read(size)
                            if data:
                                self.current += len(data)
                                self.session_b += len(data)
                                now = time.time()
                                if (now - self.last_up >= 3) or (self.current >= self.total_len):
                                    self.last_up = now
                                    elapsed = now - self.start_t
                                    speed = self.session_b / elapsed if elapsed > 0 else 0
                                    eta = (self.total_len - self.current) / speed if speed > 0 else 0

                                    db_execute("UPDATE transfers SET uploaded_bytes=? WHERE task_id=?", (self.current, task_id))
                                    asyncio.run_coroutine_threadsafe(
                                        send_progress(self.current, self.total_len, speed, eta, "Uploading to Internet Archive"),
                                        bot_client.loop
                                    )
                            return data

                    stream = ProgressStream(f, actual_size, uploaded_bytes)

                    def do_put():
                        # If resuming, supply the Content-Range header supported by S3
                        put_headers = headers.copy()
                        if uploaded_bytes > 0:
                            put_headers["Content-Range"] = f"bytes {uploaded_bytes}-{actual_size - 1}/{actual_size}"

                        resp = requests.put(upload_url, data=stream, headers=put_headers, timeout=120)
                        if resp.status_code not in [200, 201]:
                            # Fallback to standard full upload via IA library if ranged put is rejected
                            item = ia.get_item(task_id)
                            item.upload(
                                {file_name: local_path},
                                metadata={"title": clean_title, "mediatype": "movies", "collection": "opensource_movies"},
                                access_key=IA_ACCESS,
                                secret_key=IA_SECRET,
                                verify=True,
                                verbose=False
                            )

                    await bot_client.loop.run_in_executor(None, do_put)

                archive_url = f"https://archive.org/details/{task_id}"
                uploaded_files_db.append({
                    "id": task_id,
                    "title": clean_title,
                    "size": format_size(actual_size),
                    "url": archive_url
                })
                add_log(f"Upload completed: {archive_url}")

                if os.path.exists(local_path):
                    os.remove(local_path)
                db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))

                await bot_client.edit_message(
                    chat_id,
                    status_msg_id,
                    f"✅ **Upload Complete!**\n\n"
                    f"🎬 **File Name:** `{file_name}`\n"
                    f"📦 **Size:** `{format_size(actual_size)}`\n"
                    f"▶️ **Play Online:** {archive_url}",
                    buttons=None,
                    link_preview=True
                )

        except Exception as e:
            if "TRANSFER_CANCELLED" in str(e) or ctrl.get("cancel"):
                add_log(f"Task {task_id} successfully cancelled.")
                if os.path.exists(local_path):
                    os.remove(local_path)
                db_execute("DELETE FROM transfers WHERE task_id=?", (task_id,))
                await bot_client.edit_message(chat_id, status_msg_id, "❌ **Task Cancelled.**", buttons=None)
            else:
                add_log(f"Transfer error on {task_id}: {str(e)}")
                await bot_client.edit_message(chat_id, status_msg_id, f"❌ **Error:** `{str(e)}`", buttons=None)
        finally:
            gc.collect()

# ==============================================================================
# RESTART RECOVERY (Auto-Restores Queued & In-Progress Tasks in Order)
# ==============================================================================
async def resume_interrupted_tasks(bot_client):
    await asyncio.sleep(2)
    rows = db_execute("SELECT task_id FROM transfers ORDER BY created_at ASC")
    for r in rows:
        t_id = r[0]
        add_log(f"Resuming queued/interrupted task: {t_id}")
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
            <h2 style="margin:0;">🚀 Internet Archive Hub V3 (Multi-Queue & Resumable)</h2>
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
        "⚡ **Telegram to Internet Archive Hub V3 Ready!**\n\n"
        "• **Multi-File Queue:** Send multiple files at once; they will be queued and processed safely without crashing.\n"
        "• **Persistent Resume:** If the bot restarts at 80%, it resumes directly from 80% without starting from zero.\n"
        "• **Inline Controls:** Pause, Resume, or Cancel anytime with the buttons below."
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

    add_log("Telegram Media Uploader V3 online (Multi-file Safe + Resumable).")
    asyncio.run(main())
