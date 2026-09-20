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
import email
import re
import urllib.parse
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

CHUNK_SIZE = 256 * 1024  # 256 KB smooth buffer
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
# DATABASE MANAGEMENT (WAL mode for restart persistence)
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
            source_type TEXT,
            url_source TEXT,
            file_name TEXT,
            target_filename TEXT,
            clean_base TEXT,
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
# FORMATTING UTILITIES & INLINE BUTTONS (Cancel Only)
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

def make_keyboard(task_id):
    return [
        [Button.inline("❌ Cancel", data=f"cancel:{task_id}")]
    ]

# ==============================================================================
# SAFE TELEGRAM EDIT (Prevents FloodWait 429)
# ==============================================================================
last_telegram_edit_time = {}

async def safe_edit_message(bot_client, chat_id, message_id, text, buttons=None, force=False):
    now = time.time()
    last_time = last_telegram_edit_time.get(message_id, 0)
    if not force and (now - last_time < 4.0):
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
# PROGRESS FILE WRAPPER FOR ARCHIVE.ORG
# ==============================================================================
class ProgressFileReader(object):
    def __init__(self, filepath, total_size, task_id, chat_id, status_msg_id, bot_client, loop):
        self._file = open(filepath, 'rb')
        self.total_size = total_size
        self.bytes_read = 0
        self.task_id = task_id
        self.chat_id = chat_id
        self.status_msg_id = status_msg_id
        self.bot_client = bot_client
        self.loop = loop
        self.start_time = time.time()
        self.last_update = self.start_time

    def read(self, size=-1):
        if self.task_id in active_tasks:
            ctrl = active_tasks[self.task_id]
            if ctrl.get("cancel"):
                raise Exception("TRANSFER_CANCELLED")

        data = self._file.read(size)
        if data:
            self.bytes_read += len(data)
            now = time.time()
            if (now - self.last_update >= 4.0) or (self.bytes_read >= self.total_size):
                self.last_update = now
                pct = min(100.0, (self.bytes_read / self.total_size) * 100 if self.total_size > 0 else 0)
                filled = int(pct / 10)
                bar = "■" * filled + "□" * (10 - filled)
                elapsed = now - self.start_time
                speed = self.bytes_read / elapsed if elapsed > 0 else 0
                eta_seconds = (self.total_size - self.bytes_read) / speed if speed > 0 else 0

                text = (
                    f"🚀 **Uploading to Internet Archive**\n\n"
                    f"`[{bar}]` **{pct:.1f}%**\n\n"
                    f"⚡ **Speed:** `{format_size(speed)}/s`\n"
                    f"📁 **Uploaded:** `{format_size(self.bytes_read)}` / `{format_size(self.total_size)}`\n"
                    f"⏳ **ETA:** `{format_eta(eta_seconds)}`"
                )
                db_execute("UPDATE transfers SET uploaded_bytes=? WHERE task_id=?", (self.bytes_read, self.task_id))
                asyncio.run_coroutine_threadsafe(
                    safe_edit_message(
                        self.bot_client, self.chat_id, self.status_msg_id, text,
                        buttons=make_keyboard(self.task_id)
                    ),
                    self.loop
                )
        return data

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
# PIPELINE: TELEGRAM / DIRECT LINK DOWNLOAD & ARCHIVE.ORG UPLOAD
# ==============================================================================
async def execute_transfer(task_id, bot_client):
    rows = db_execute("SELECT * FROM transfers WHERE task_id=?", (task_id,))
    if not rows:
        return

    r = rows[0]
    chat_id, msg_id, status_msg_id = r[1], r[2], r[3]
    source_type, url_source = r[4], r[5]
    file_name, target_filename, clean_base = r[6], r[7], r[8]
    total_size, stage, local_path = r[9], r[12], r[14]

    if task_id not in active_tasks:
        active_tasks[task_id] = {"cancel": False}

    ctrl = active_tasks[task_id]
    main_loop = asyncio.get_running_loop()

    async with queue_semaphore:
        async def send_download_progress(curr, tot, speed, eta, source_label, force=False):
            pct = min(100.0, (curr / tot * 100) if tot > 0 else 0)
            filled = int(pct / 10)
            bar = "■" * filled + "□" * (10 - filled)
            tot_str = format_size(tot) if tot > 0 else "Calculating..."
            text = (
                f"📥 **Downloading from {source_label}**\n\n"
                f"`[{bar}]` **{pct:.1f}%**\n\n"
                f"⚡ **Speed:** `{format_size(speed)}/s`\n"
                f"📁 **Downloaded:** `{format_size(curr)}` / `{tot_str}`\n"
                f"⏳ **ETA:** `{format_eta(eta)}`"
            )
            await safe_edit_message(
                bot_client, chat_id, status_msg_id, text,
                buttons=make_keyboard(task_id), force=force
            )

        try:
            # ------------------------------------------------------------------
            # STAGE 1: DOWNLOAD STAGE
            # ------------------------------------------------------------------
            if stage == "downloading":
                current_size = 0
                if os.path.exists(local_path):
                    current_size = os.path.getsize(local_path)
                    add_log(f"Resuming download {task_id} at {format_size(current_size)}")

                start_time = time.time()
                downloaded_session = 0

                # OPTION A: TELEGRAM DOWNLOAD
                if source_type == "telegram":
                    original_msg = await bot_client.get_messages(chat_id, ids=msg_id)
                    if not original_msg or not original_msg.media:
                        add_log(f"Task {task_id} media missing.")
                        return

                    with open(local_path, "ab") as f:
                        async for chunk in bot_client.iter_download(original_msg.media, offset=current_size, chunk_size=CHUNK_SIZE):
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
                            await send_download_progress(current_size, total_size, speed, eta, "Telegram")

                # OPTION B: DIRECT LINK DOWNLOAD (Zero-Stall Stream Engine)
                elif source_type == "direct_url":
                    add_log(f"Starting direct stream download for {task_id}: {url_source}")
                    
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                        "Connection": "keep-alive"
                    }
                    if current_size > 0:
                        headers["Range"] = f"bytes={current_size}-"

                    def do_direct_download():
                        nonlocal current_size, downloaded_session, total_size, file_name, target_filename, clean_base
                        session = requests.Session()
                        
                        # Follow redirects explicitly first to obtain active direct stream endpoint
                        resp = session.get(url_source, headers=headers, stream=True, allow_redirects=True, timeout=30)
                        resp.raise_for_status()

                        # Determine size from headers
                        content_len = resp.headers.get("Content-Length")
                        if content_len:
                            total_size = int(content_len) + (current_size if "bytes=" in headers.get("Range", "") else 0)
                            db_execute("UPDATE transfers SET total_size=? WHERE task_id=?", (total_size, task_id))

                        # Extract filename if present
                        cd = resp.headers.get("Content-Disposition", "")
                        if "filename=" in cd:
                            match = re.findall(r'filename="?([^";]+)"?', cd)
                            if match:
                                file_name = match[0].strip()
                                clean_base = os.path.splitext(file_name)[0]
                                target_filename = f"{clean_base}.mp4"
                                db_execute(
                                    "UPDATE transfers SET file_name=?, target_filename=?, clean_base=? WHERE task_id=?",
                                    (file_name, target_filename, clean_base, task_id)
                                )

                        add_log(f"Connected! Writing stream chunks to {local_path}...")
                        last_ui_update = time.time()

                        mode = "ab" if current_size > 0 else "wb"
                        with open(local_path, mode) as f:
                            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                                if ctrl.get("cancel"):
                                    resp.close()
                                    raise Exception("TRANSFER_CANCELLED")
                                if not chunk:
                                    continue

                                f.write(chunk)
                                current_size += len(chunk)
                                downloaded_session += len(chunk)

                                now = time.time()
                                if (now - last_ui_update >= 3.5) or (total_size > 0 and current_size >= total_size):
                                    last_ui_update = now
                                    elapsed = now - start_time
                                    speed = downloaded_session / elapsed if elapsed > 0 else 0
                                    eta = (total_size - current_size) / speed if (speed > 0 and total_size > current_size) else 0

                                    db_execute("UPDATE transfers SET downloaded_bytes=? WHERE task_id=?", (current_size, task_id))
                                    asyncio.run_coroutine_threadsafe(
                                        send_download_progress(current_size, total_size, speed, eta, "Direct Link"),
                                        main_loop
                                    )

                    await main_loop.run_in_executor(None, do_direct_download)

                stage = "uploading"
                db_execute("UPDATE transfers SET stage='uploading' WHERE task_id=?", (task_id,))
                add_log(f"Download complete: {local_path}")

            # ------------------------------------------------------------------
            # STAGE 2: ARCHIVE.ORG UPLOAD (.mp4 & format: h.264)
            # ------------------------------------------------------------------
            if stage == "uploading":
                if not os.path.exists(local_path):
                    add_log(f"Local file missing for {task_id}. Re-queuing download.")
                    db_execute("UPDATE transfers SET stage='downloading', downloaded_bytes=0 WHERE task_id=?", (task_id,))
                    asyncio.create_task(execute_transfer(task_id, bot_client))
                    return

                actual_size = os.path.getsize(local_path)
                item_id = f"tg_{uuid.uuid4().hex[:8]}"

                add_log(f"Starting upload for '{target_filename}' ({format_size(actual_size)}) to item {item_id}...")
                await safe_edit_message(
                    bot_client, chat_id, status_msg_id,
                    "🚀 **Starting upload to Internet Archive...**",
                    buttons=make_keyboard(task_id),
                    force=True
                )

                item = ia.get_item(item_id)

                def perform_upload():
                    with ProgressFileReader(local_path, actual_size, task_id, chat_id, status_msg_id, bot_client, main_loop) as progress_file:
                        item.upload(
                            {target_filename: progress_file},
                            metadata={
                                "title": clean_base,
                                "mediatype": "movies",
                                "collection": "opensource_movies",
                                "format": "h.264"
                            },
                            access_key=IA_ACCESS,
                            secret_key=IA_SECRET
                        )

                await main_loop.run_in_executor(None, perform_upload)

                archive_url = f"https://archive.org/details/{item_id}"
                uploaded_files_db.append({
                    "id": item_id,
                    "title": clean_base,
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
                    f"🎬 **File Name:** `{target_filename}`\n"
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
# RESTART RECOVERY WORKER (Restores Transfers on Reboot)
# ==============================================================================
async def resume_interrupted_tasks(bot_client):
    await asyncio.sleep(3)
    rows = db_execute("SELECT task_id FROM transfers ORDER BY created_at ASC")
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
            <h2 style="margin:0;">🚀 Internet Archive Hub</h2>
            <button class="btn" onclick="location.reload()">Refresh</button>
        </div>

        <div class="card">
            <h3 style="margin-top:0;">📁 Uploaded Files & Metadata</h3>
            <table>
                <thead>
                    <tr>
                        <th>Title / File</th>
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
# TELEGRAM EVENT HANDLERS
# ==============================================================================
bot = TelegramClient('tg_archive_session', API_ID, API_HASH)

@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    await event.reply(
        "⚡ **Telegram & Direct Link Uploader Ready!**\n\n"
        "• Send any video or `.mkv` file.\n"
        "• **Direct Link Support:** Send any direct download link (including shortened URLs like `clck.ru`).\n"
        "• Automatically downloads and uploads to Archive.org as streamable `.mp4`."
    )

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode("utf-8")
    action, task_id = data.split(":")

    if task_id not in active_tasks:
        await event.answer("Task is no longer active or in queue.", alert=True)
        return

    ctrl = active_tasks[task_id]

    if action == "cancel":
        ctrl["cancel"] = True
        await event.answer("Cancelling task...")
        add_log(f"Task {task_id} cancelled by user.")

@bot.on(events.NewMessage)
async def media_or_link_handler(event):
    msg_text = (event.message.message or "").strip()
    if msg_text.startswith("/"):
        return

    # 1. HANDLE DIRECT HTTP / HTTPS DOWNLOAD LINKS
    url_match = re.search(r"(https?://[^\s]+)", msg_text)
    if url_match:
        url = url_match.group(1)
        task_id = f"url_{uuid.uuid4().hex[:8]}"

        status_msg = await event.reply(
            "⏳ **Connecting to Direct Download Link...**",
            buttons=make_keyboard(task_id)
        )

        initial_name = f"video_{uuid.uuid4().hex[:6]}.mkv"
        target_filename = f"{os.path.splitext(initial_name)[0]}.mp4"
        local_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_{initial_name}")

        db_execute(
            '''INSERT INTO transfers (
                task_id, chat_id, msg_id, status_msg_id, source_type, url_source,
                file_name, target_filename, clean_base, total_size, downloaded_bytes,
                uploaded_bytes, stage, status, local_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (task_id, event.chat_id, event.message.id, status_msg.id, "direct_url", url,
             initial_name, target_filename, os.path.splitext(initial_name)[0], 0, 0, 0,
             "downloading", "active", local_path, time.time())
        )

        active_tasks[task_id] = {"cancel": False}
        add_log(f"Queued direct link: '{url}'")
        asyncio.create_task(execute_transfer(task_id, bot))
        return

    # 2. HANDLE TELEGRAM MEDIA FILES
    if event.message.media:
        raw_name = None
        if hasattr(event.message.media, "document") and event.message.media.document:
            for attr in event.message.media.document.attributes:
                if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                    raw_name = attr.file_name
                    break

        if not raw_name:
            raw_name = f"video_{uuid.uuid4().hex[:6]}.mkv"

        clean_base = os.path.splitext(raw_name)[0]
        target_filename = f"{clean_base}.mp4"
        file_size = event.message.file.size if event.message.file else 0

        if file_size > 2000 * 1024 * 1024:
            await event.reply("⚠️ File exceeds Telegram's 2.00 GB bot limit.")
            return

        task_id = f"tg_{uuid.uuid4().hex[:8]}"
        local_path = os.path.join(DOWNLOAD_DIR, f"{task_id}_{raw_name}")

        status_msg = await event.reply(
            "⏳ **Queued for transfer...**",
            buttons=make_keyboard(task_id)
        )

        db_execute(
            '''INSERT INTO transfers (
                task_id, chat_id, msg_id, status_msg_id, source_type, url_source,
                file_name, target_filename, clean_base, total_size, downloaded_bytes,
                uploaded_bytes, stage, status, local_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (task_id, event.chat_id, event.message.id, status_msg.id, "telegram", "",
             raw_name, target_filename, clean_base, file_size, 0, 0, "downloading",
             "active", local_path, time.time())
        )

        active_tasks[task_id] = {"cancel": False}
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

    add_log("Telegram & Direct Link Media Uploader online (Smooth Stream Engine).")
    asyncio.run(main())
