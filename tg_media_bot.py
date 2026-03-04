import re
import os
import time
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient, events


load_dotenv(override=True)

# --- CONFIGURATION ---
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Directory Paths (Updated per instructions)
DIRECTORIES = {
    '/mv': '/mnt/blue/movies',
    '/tv': '/mnt/blue/tv'
}

# Constraints
MAX_CONCURRENT_DOWNLOADS = 2   # [cite: 54]
MAX_FILE_SIZE_GB = 20          # [cite: 69]
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024

# --- STATE MANAGEMENT ---
queue = asyncio.Queue()
active_downloads = {} # Format: {msg_id: asyncio.Task}
current_concurrent_count = 0

# Initialize Client
client = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- HELPER FUNCTIONS ---

def format_bytes(size):
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"


def sanitize_filename(filename):
    """
    Sanitizes filename to prevent filesystem errors.
    ONLY removes invalid characters (like / \ : * ? " < > |).
    Preserves spaces, brackets, and original formatting.
    """
    # Remove ONLY invalid filesystem characters
    filename = re.sub(r'[\\/*?:"<>|]', '', filename)
    
    # Remove newlines (critical if caption has multiple lines)
    filename = filename.replace('\n', ' ').replace('\r', '')
    
    # Strip leading/trailing whitespace
    return filename.strip()


async def progress_bar(current, total, event, start_time, last_update_time):
    now = time.time()
    if (now - last_update_time[0]) < 3 and current != total:
        return
    last_update_time[0] = now
    elapsed_time = now - start_time
    
    speed = current / elapsed_time if elapsed_time > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    
    percentage = (current * 100) / total
    completed_blocks = int(percentage // 10)
    progress_str = "🟦" * completed_blocks + "⬜" * (10 - completed_blocks)

    text = (
        f"**Downloading...**\n"
        f"{progress_str} **{percentage:.1f}%**\n"
        f"💾 `{format_bytes(current)} / {format_bytes(total)}`\n"
        f"🚀 `{format_bytes(speed)}/s` | ⏳ `{int(eta)}s`\n\n"
        f"reply `/cancel` to stop."
    )
    try:
        await event.edit(text)
    except:
        pass

# --- CORE LOGIC ---

async def download_worker():
    """
    Background worker that processes the queue. [cite: 53]
    """
    global current_concurrent_count
    
    while True:
        # Wait for a job from the queue
        job = await queue.get()
        
        # Wait until a slot opens up [cite: 57]
        while current_concurrent_count >= MAX_CONCURRENT_DOWNLOADS:
            await asyncio.sleep(5)

        current_concurrent_count += 1
        
        # Unpack job
        event, reply_msg, download_path, filename = job
        status_msg = await event.reply(f"⬇️ **Starting Download:** `{filename}`")
        
        # Store task for cancellation [cite: 87]
        task = asyncio.create_task(
            perform_download(status_msg, reply_msg, download_path, filename)
        )
        active_downloads[status_msg.id] = task
        
        try:
            await task
        except asyncio.CancelledError:
            pass # Handled inside perform_download
        finally:
            current_concurrent_count -= 1
            if status_msg.id in active_downloads:
                del active_downloads[status_msg.id]
            queue.task_done()


async def perform_download(status_msg, reply_msg, folder_path, clean_name):
    """
    Executes the actual download logic with .part handling.
    """
    # ensure directory exists [cite: 23]
    os.makedirs(folder_path, exist_ok=True)

    # 1. Define paths
    final_path = os.path.join(folder_path, clean_name)
    temp_path = final_path + ".part" # [cite: 4]

    # 2. Duplicate Check [cite: 93]
    if os.path.exists(final_path):
        await status_msg.edit(f"❌ **Error:** File already exists.\n`{clean_name}`")
        return

    start_time = time.time()
    last_update = [0]

    try:
        # 3. Download to .part [cite: 4]
        await reply_msg.download_media(
            file=temp_path,
            progress_callback=lambda c, t: client.loop.create_task(
                progress_bar(c, t, status_msg, start_time, last_update)
            )
        )

        # 4. Rename on Success [cite: 8]
        os.rename(temp_path, final_path)
        
        await status_msg.edit(
            f"✅ **Download Complete!**\n"
            f"📄 `{clean_name}`\n"
            f"📂 `{folder_path}`"
        )

    except asyncio.CancelledError:
        # 6. Cleanup on Cancel 
        if os.path.exists(temp_path):
            os.remove(temp_path)
        await status_msg.edit("❌ **Download Cancelled.**")
        
    except Exception as e:
        # Cleanup on Failure [cite: 12]
        if os.path.exists(temp_path):
            os.remove(temp_path)
        await status_msg.edit(f"❌ **Failed:** `{str(e)}`")

# --- COMMAND HANDLERS ---


@client.on(events.NewMessage(pattern=r'^/cancel$'))
async def cancel_handler(event):
    """
    Cancels the download the user replied to. 
    """
    if not event.is_reply:
        await event.reply("⚠️ Reply to the **download status message** to cancel it.")
        return

    reply_msg = await event.get_reply_message()
    task = active_downloads.get(reply_msg.id)

    if task:
        task.cancel() # [cite: 82]
        await event.reply("🛑 Task Cancelled!")
    else:
        await event.reply("⚠️ No active download found for that message.")


@client.on(events.NewMessage(pattern=r'^/(mv|tv)$'))
async def enqueue_handler(event):
    if not event.is_reply:
        await event.reply("❌ Reply to a file.")
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg.media:
        await event.reply("❌ No media found.")
        return

    # 1. Size Limit Check
    if reply_msg.file.size > MAX_FILE_SIZE_BYTES:
        await event.reply(f"❌ **File too large.**\nLimit: {MAX_FILE_SIZE_GB}GB")
        return

    # 2. Determine Path
    cmd = event.raw_text.strip().lower()
    target_dir = DIRECTORIES.get(cmd)

    # 3. Filename Extraction (Priority: Caption > Metadata Name > Timestamp)
    possible_name = reply_msg.text.strip().replace("_", " ").strip()
    
    if not possible_name:
        possible_name = reply_msg.file.name.strip().replace("_", "").strip()
    
    if not possible_name:
        possible_name = f"Unknown_File_{int(time.time())}"

    # 4. Extension Handling (Force .mkv)
    if ".mkv" in possible_name.strip().lower():
        possible_name += ".mkv"

    # 5. Sanitize
    clean_name = sanitize_filename(possible_name.strip())

    # 6. Add to Queue
    position = queue.qsize() + 1
    await queue.put((event, reply_msg, target_dir, clean_name))

    if current_concurrent_count >= MAX_CONCURRENT_DOWNLOADS:
        await event.reply(f"⏳ **Added to Queue** (Position: {position})\nWait for a slot...")
        

# Start the worker loop
client.loop.create_task(download_worker())

print("Bot is running with Queue System...")
client.run_until_disconnected()

