import os
import re
import time
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

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

# --- LIVE WEB LOG CONSOLE ---
logs_history = []


def add_log(msg):
  timestamp = time.strftime("%H:%M:%S")
  entry = f"[{timestamp}] {msg}"
  print(entry)
  logs_history.append(entry)
  if len(logs_history) > 60:
    logs_history.pop(0)


class DashboardHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.send_header("Content-type", "text/html; charset=utf-8")
    self.end_headers()

    log_rows = "".join([
        f"<div class='log-row'>{log}</div>"
        for log in reversed(logs_history)
    ]) or "<div class='log-row'>No activity recorded yet...</div>"

    html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Archive Uploader Console</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta http-equiv="refresh" content="3">
            <style>
                body {{ background: #0b0f19; color: #e2e8f0; font-family: monospace; padding: 20px; margin: 0; }}
                .container {{ max-width: 850px; margin: 0 auto; }}
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
                    <h2>🤖 Archive Direct Uploader</h2>
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


# --- HELPER FUNCTIONS ---
def format_size(bytes_size):
  for unit in ["B", "KB", "MB", "GB"]:
    if bytes_size < 1024:
      return f"{bytes_size:.2f} {unit}"
    bytes_size /= 1024
  return f"{bytes_size:.2f} TB"


# --- TELEGRAM HANDLERS ---
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
  add_log(f"Chat {message.chat.id} sent /start")
  bot.reply_to(
      message,
      "👋 **Direct Internet Archive Uploader**\n\n"
      "Send any direct video/file download link (HTTP/HTTPS) to stream and"
      " upload it to the Internet Archive with no file size limits.\n\n"
      "💡 *Tip: For Telegram files over 20MB, generate a direct link using a"
      " file-to-link bot and paste it here!*",
      parse_mode="Markdown",
  )


@bot.message_handler(content_types=["video", "document"])
def handle_telegram_media(message):
  media = message.video or message.document
  if media and media.file_size and media.file_size > 20 * 1024 * 1024:
    add_log(
        f"Rejected direct Telegram file: {media.file_size} bytes exceeds 20MB"
        " Telegram limit"
    )
    bot.reply_to(
        message,
        f"⚠️ **File is too large ({format_size(media.file_size)}) for direct"
        " Telegram forwarding!**\n\n"
        "To upload files without any limit:\n"
        "1. Forward this file to a link bot (like `@FileStreamBot` or"
        " `@DirectLinkGeneratorBot`)\n"
        "2. Copy the generated direct link\n"
        "3. Paste the link here, and it will upload immediately!",
        parse_mode="Markdown",
    )
    return

  # For files under 20MB
  status = bot.reply_to(message, "📥 Downloading file from Telegram...")
  try:
    file_info = bot.get_file(media.file_id)
    downloaded = bot.download_file(file_info.file_path)

    item_id = f"tg_upload_{uuid.uuid4().hex[:8]}"
    file_path = f"/tmp/{item_id}.mp4"

    with open(file_path, "wb") as f:
      f.write(downloaded)

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

    archive_url = f"https://archive.org/details/{item_id}"
    add_log(f"Upload Complete: {archive_url}")
    bot.edit_message_text(
        f"✅ **Upload Complete!**\n\n🔗 {archive_url}",
        chat_id=message.chat.id,
        message_id=status.message_id,
        parse_mode="Markdown",
    )
  except Exception as e:
    add_log(f"Error: {str(e)}")
    bot.edit_message_text(
        f"❌ **Error:** {str(e)}",
        chat_id=message.chat.id,
        message_id=status.message_id,
    )
  finally:
    if os.path.exists(file_path):
      os.remove(file_path)


@bot.message_handler(func=lambda msg: True)
def handle_direct_urls(message):
  text = message.text.strip()
  url_match = re.search(r"(https?://[^\s]+)", text)

  if not url_match:
    bot.reply_to(
        message, "⚠️ Send a valid direct download link (e.g. `http.../video.mp4`)"
    )
    return

  url = url_match.group(0)

  if "t.me/" in url:
    bot.reply_to(
        message,
        "❌ `t.me/...` links are Telegram web previews, not direct download"
        " links.",
    )
    return

  add_log(f"Starting direct URL download: {url}")
  status = bot.reply_to(
      message, "⚡ **Connecting to URL & calculating size...**"
  )

  item_id = f"archive_video_{uuid.uuid4().hex[:8]}"
  file_path = f"/tmp/{item_id}.mp4"

  try:
    start_time = time.time()
    last_update = [start_time]

    with requests.get(url, stream=True, timeout=60) as r:
      r.raise_for_status()
      total_size = int(r.headers.get("content-length", 0))
      downloaded = 0

      with open(file_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
          if chunk:
            f.write(chunk)
            downloaded += len(chunk)

            # Update progress bar every 3 seconds
            now = time.time()
            if now - last_update[0] >= 3:
              last_update[0] = now
              elapsed = now - start_time
              speed = downloaded / elapsed if elapsed > 0 else 0

              if total_size > 0:
                pct = (downloaded / total_size) * 100
                filled = int(pct / 10)
                bar = "■" * filled + "□" * (10 - filled)
                eta = (total_size - downloaded) / speed if speed > 0 else 0
                progress_text = (
                    f"📥 **Downloading File...**\n\n"
                    f"`[{bar}]` **{pct:.1f}%**\n\n"
                    f"⚡ **Speed:** `{format_size(speed)}/s`\n"
                    f"📁 **Size:** `{format_size(downloaded)}` /"
                    f" `{format_size(total_size)}`\n"
                    f"⏳ **ETA:** `{int(eta)}s`"
                )
              else:
                progress_text = (
                    f"📥 **Downloading Stream...**\n\n"
                    f"⚡ **Speed:** `{format_size(speed)}/s`\n"
                    f"📁 **Downloaded:** `{format_size(downloaded)}`"
                )

              try:
                bot.edit_message_text(
                    progress_text,
                    chat_id=message.chat.id,
                    message_id=status.message_id,
                    parse_mode="Markdown",
                )
              except Exception:
                pass

    add_log(f"Downloaded {file_path}. Uploading to Internet Archive...")
    bot.edit_message_text(
        "🚀 **Uploading to Internet Archive... Please wait.**",
        chat_id=message.chat.id,
        message_id=status.message_id,
        parse_mode="Markdown",
    )

    item = ia.get_item(item_id)
    item.upload(
        file_path,
        metadata={"title": f"Upload {item_id}", "mediatype": "movies"},
        access_key=IA_ACCESS,
        secret_key=IA_SECRET,
    )

    archive_url = f"https://archive.org/details/{item_id}"
    add_log(f"Upload Complete: {archive_url}")
    bot.edit_message_text(
        f"✅ **Upload Complete!**\n\n"
        f"🔗 **Archive Link:** {archive_url}\n"
        f"📦 **Size:** `{format_size(os.path.getsize(file_path))}`",
        chat_id=message.chat.id,
        message_id=status.message_id,
        parse_mode="Markdown",
    )

  except Exception as e:
    add_log(f"Upload failed: {str(e)}")
    bot.edit_message_text(
        f"❌ **Error:** {str(e)}",
        chat_id=message.chat.id,
        message_id=status.message_id,
    )
  finally:
    if os.path.exists(file_path):
      os.remove(file_path)


if __name__ == "__main__":
  # Start web server for Render
  t = threading.Thread(target=run_web, daemon=True)
  t.start()

  # Clear any old conflicting webhooks
  try:
    bot.remove_webhook()
    time.sleep(1)
  except Exception:
    pass

  add_log("Bot started successfully. Listening for links and files...")
  bot.infinity_polling(skip_pending=True)
