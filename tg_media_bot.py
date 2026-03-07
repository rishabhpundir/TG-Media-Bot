import re
import os
import time
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events, utils

load_dotenv(override=True)

# --- CONFIGURATION ---
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Directory Paths
DIRECTORIES = {
    '/mv': '/mnt/blue/movies',
    '/tv': '/mnt/blue/tv',
    '/lmv': '/mnt/blue/movies', # Link variants
    '/ltv': '/mnt/blue/tv'
}

# Constraints
MAX_CONCURRENT_DOWNLOADS = 2
MAX_FILE_SIZE_GB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024

# --- STATE MANAGEMENT ---
queue = asyncio.Queue()
active_downloads = {} 
current_concurrent_count = 0

# --- INITIALIZE DUAL CLIENTS ---
# 1. The Bot (Interacts with you)
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# 2. The Userbot (Interacts with restricted channels)
userbot = TelegramClient('user_session', API_ID, API_HASH)

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
    Sanitizes filename. Only removes invalid chars. 
    Preserves spaces and brackets.
    """
    # Remove ONLY invalid filesystem characters
    filename = re.sub(r'[\\/*?:"<>|]', '', filename)
    filename = filename.replace('\n', ' ').replace('\r', '')
    return filename.strip()

def ensure_mkv_extension(filename):
    """Ensures file ends in .mkv without duplication"""
    if not filename.lower().endswith(".mkv"):
        filename += ".mkv"
    return filename

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

# --- CORE WORKER LOGIC ---

async def download_worker():
    global current_concurrent_count
    
    while True:
        job = await queue.get()
        
        while current_concurrent_count >= MAX_CONCURRENT_DOWNLOADS:
            await asyncio.sleep(5)

        current_concurrent_count += 1
        
        # Unpack job. 
        # 'media_msg' is the message containing the file (might be from Bot OR Userbot)
        event, media_msg, download_path, filename = job
        
        status_msg = await event.reply(f"⬇️ **Starting Download:** `{filename}`")
        
        task = asyncio.create_task(
            perform_download(status_msg, media_msg, download_path, filename)
        )
        active_downloads[status_msg.id] = task
        
        try:
            await task
        except asyncio.CancelledError:
            pass 
        finally:
            current_concurrent_count -= 1
            if status_msg.id in active_downloads:
                del active_downloads[status_msg.id]
            queue.task_done()

async def perform_download(status_msg, media_msg, folder_path, clean_name):
    os.makedirs(folder_path, exist_ok=True)
    final_path = os.path.join(folder_path, clean_name)
    temp_path = final_path + ".part"

    if os.path.exists(final_path):
        await status_msg.edit(f"❌ **Error:** File already exists.\n`{clean_name}`")
        return

    start_time = time.time()
    last_update = [0]

    try:
        # download_media automatically uses the client that fetched the message
        # So if media_msg came from userbot, userbot downloads it.
        await media_msg.download_media(
            file=temp_path,
            progress_callback=lambda c, t: bot.loop.create_task(
                progress_bar(c, t, status_msg, start_time, last_update)
            )
        )

        os.rename(temp_path, final_path)
        await status_msg.edit(f"✅ **Download Complete!**\n📄 `{clean_name}`\n📂 `{folder_path}`")

    except asyncio.CancelledError:
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit("❌ **Download Cancelled.**")
    except Exception as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit(f"❌ **Failed:** `{str(e)}`")

# --- HANDLERS ---

@bot.on(events.NewMessage(pattern=r'^/cancel$'))
async def cancel_handler(event):
    if not event.is_reply:
        await event.reply("⚠️ Reply to the **download status message** to cancel it.")
        return
    reply_msg = await event.get_reply_message()
    task = active_downloads.get(reply_msg.id)
    if task:
        task.cancel()
        await event.reply("🛑 Task Cancelled!")
    else:
        await event.reply("⚠️ No active download found.")

# 1. STANDARD HANDLER (/mv, /tv)
@bot.on(events.NewMessage(pattern=r'^/(mv|tv)$'))
async def standard_handler(event):
    if not event.is_reply: return await event.reply("❌ Reply to a file.")
    reply_msg = await event.get_reply_message()
    if not reply_msg.media: return await event.reply("❌ No media found.")

    if reply_msg.file.size > MAX_FILE_SIZE_BYTES:
        return await event.reply(f"❌ **File too large.**\nLimit: {MAX_FILE_SIZE_GB}GB")

    cmd = event.raw_text.strip().lower()
    target_dir = DIRECTORIES.get(cmd)

    # Filename Logic
    possible_name = reply_msg.text.strip().replace("_", " ").strip()
    if not possible_name: possible_name = reply_msg.file.name.strip().replace("_", " ").strip()
    if not possible_name: possible_name = f"Unknown_File_{int(time.time())}"
    
    clean_name = ensure_mkv_extension(sanitize_filename(possible_name))

    position = queue.qsize() + 1
    await queue.put((event, reply_msg, target_dir, clean_name))

    if current_concurrent_count >= MAX_CONCURRENT_DOWNLOADS:
        await event.reply(f"⏳ **Queued (Standard)** (Pos: {position})")


# 2. LINK HANDLER (/lmv, /ltv) (Fixed Regex & Logic)
@bot.on(events.NewMessage(pattern=r'^/l(mv|tv)'))  # Matches /lmv <link>
async def link_handler(event):
    """
    Handles links by asking the Userbot to fetch the message.
    """
    text_split = event.text.strip().split(maxsplit=1)
    if len(text_split) < 2:
        return await event.reply("❌ Usage: `/lmv <link>` or `/ltv <link>`")

    link_text = text_split[1]
    
    # Regex to extract (channel_id, msg_id) from https://t.me/c/12345/6789 or t.me/username/6789
    match = re.search(r't\.me/(?:c/)?(\w+|\d+)/(\d+)', link_text)
    
    if not match:
        return await event.reply("❌ No valid Telegram link found.")

    identifier = match.group(1) # Username or ID
    msg_id = int(match.group(2))
    
    status = await event.reply(f"🕵️ **Userbot:** Fetching message `{msg_id}` from `{identifier}`...")

    try:
        # Determine valid entity for Userbot
        if identifier.isdigit():
            # Private channel ID usually needs -100 prefix for API
            entity = int(f"-100{identifier}")
        else:
            # Public username
            entity = identifier

        # USERBOT ACTION: Get the actual message object containing the file
        restricted_msg = await userbot.get_messages(entity, ids=msg_id)

        if not restricted_msg or not restricted_msg.media:
             return await status.edit("❌ Userbot found the message, but it has no media.")
        
        # Size check
        if restricted_msg.file.size > MAX_FILE_SIZE_BYTES:
            return await status.edit(f"❌ **File too large.**")

        # Filename Logic
        possible_name = restricted_msg.text.strip().replace("_", " ").strip()
        if not possible_name: possible_name = restricted_msg.file.name.strip().replace("_", " ").strip()
        if not possible_name: possible_name = f"Restricted_File_{int(time.time())}"
        
        clean_name = ensure_mkv_extension(sanitize_filename(possible_name))

        # Queue it!
        cmd_base = text_split[0].lower().replace('/l', '/') # /lmv -> /mv
        target_dir = DIRECTORIES.get(cmd_base)
        
        position = queue.qsize() + 1
        await queue.put((event, restricted_msg, target_dir, clean_name))

        if current_concurrent_count >= MAX_CONCURRENT_DOWNLOADS:
            await status.edit(f"⏳ **Queued (Userbot)** (Pos: {position})")
        else:
            await status.delete() # Clean up status msg since downloader will send a new one

    except Exception as e:
        await status.edit(f"❌ **Userbot Error:** `{str(e)}`\nMake sure your account has joined the channel.")

# --- MAIN EXECUTION ---
async def main():
    print("Starting Userbot...")
    await userbot.start() # Prompts for phone on first run
    
    print("Starting Bot...")
    # Bot is already started via .start() above
    
    print("🚀 Dual-Client System Ready!")
    
    # FIX: Use bot.loop instead of client.loop
    bot.loop.create_task(download_worker())
    
    # Run both clients
    await asyncio.gather(
        bot.run_until_disconnected(),
        userbot.run_until_disconnected()
    )

if __name__ == '__main__':
    # Use bot.loop to run the main async function
    bot.loop.run_until_complete(main())
    
    
    
    