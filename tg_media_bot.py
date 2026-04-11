import re
import os
import time
import shlex
import shutil
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv(override=True)

# --- CONFIGURATION ---
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS_ = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [int(x.strip()) for x in ALLOWED_USERS_.split(",") if x.strip()]

# Directory Paths
DIRECTORIES = {
    '/mv': '/mnt/blue/movies',
    '/tv': '/mnt/blue/tv',
    '/lmv': '/mnt/blue/movies',
    '/ltv': '/mnt/blue/tv',
    '/mv2': '/mnt/media/movies',
    '/tv2': '/mnt/media/tv',
    '/lmv2': '/mnt/media/movies',
    '/ltv2': '/mnt/media/tv'
}

# Constraints
MAX_CONCURRENT_DOWNLOADS = 2
MAX_FILE_SIZE_GB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024

# --- STATE MANAGEMENT ---
queue = asyncio.Queue()
active_downloads = {} 
pending_deletions = {}
current_concurrent_count = 0

# --- INITIALIZE DUAL CLIENTS ---
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)    # 1. The Bot (Interacts with you)
userbot = TelegramClient('user_session', API_ID, API_HASH)                          # 2. The Userbot (Interacts with restricted channels)


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


async def progress_bar(current, total, event, start_time, last_update_time, filename):
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
        f"**Downloading:** `{filename}`\n"
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
        await media_msg.download_media(
            file=temp_path,
            progress_callback=lambda c, t: bot.loop.create_task(
                progress_bar(c, t, status_msg, start_time, last_update, clean_name)
            )
        )

        os.rename(temp_path, final_path)
        
        # Calculate final size and update message with full path in quotes
        file_size_str = format_bytes(os.path.getsize(final_path))
        await status_msg.edit(
            f"✅ **Download Complete!**\n"
            f"💾 **Size:** `{file_size_str}`\n"
            f"📂 **Path:** \"{final_path}\""
        )

    except asyncio.CancelledError:
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit("❌ **Download Cancelled.**")
    except Exception as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit(f"❌ **Failed:** `{str(e)}`")
        
        
# --- HANDLERS ---
@bot.on(events.NewMessage(pattern=r'^/start$'))
async def start_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return
        
    welcome_text = """👋 **Welcome back, Rishabh! System is online and ready.**

Here is your current command list:

📥 **Downloads:**
`/mv` / `/mv2` - Reply to file -> Save to Movies.
`/tv` / `/tv2` - Reply to file -> Save to TV.
`/lmv <link>` / `/lmv2 <link>` - Fetch restricted link -> Movies.
`/ltv <link>` / `/ltv2 <link>` - Fetch restricted link -> TV.

🗄️ **File Manager (`/fm`):**
`/fm ls` - List base directories.
`/fm ls <dir_key/path>` - View folder contents (e.g., `/fm ls tv/Breaking Bad`).
`/fm rn "<path>" "<new_name>"` - Rename a file/folder.
`/fm rm "<path>"` - Instantly delete a file/folder.

⚙️ **Task Management:**
`/cancel` - Reply to an active download or pending list to abort.
`/del` - Reply to a "Download Complete" message to delete that file.
`/del <mv|tv|mv2|tv2> <keyword1.keyword2>` - Search for and safely delete files matching keywords."""

    await event.reply(welcome_text)


@bot.on(events.NewMessage(pattern=r'^/fm(?:\s+(.*))?$'))
async def fm_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    args_str = event.pattern_match.group(1)
    
    help_text = """🗄️ **File Manager (`/fm`)**
**Usage:**
`/fm ls` - List all base directories
`/fm ls <dir_key or path>` - Traverse/list folder contents
`/fm rn "<path>" "<new_name>"` - Rename a file or folder
`/fm rm "<path>"` - Delete a file or folder

*Tip: You can use directory keys as shortcuts! (e.g., `/fm ls mv/Breaking Bad`)*"""

    if not args_str:
        return await event.reply(help_text)

    try:
        # shlex safely splits arguments, respecting quoted strings with spaces
        args = shlex.split(args_str)
    except ValueError as e:
        return await event.reply(f"❌ **Parse Error:** Check your quotes.\n`{str(e)}`")

    if not args:
        return await event.reply(help_text)

    cmd = args[0].lower()
    
    # --- HELPER: Resolve Shortcut Paths ---
    def resolve_path(path_str):
        # Allow user to type 'mv/Folder' instead of '/mnt/blue/movies/Folder'
        parts = path_str.replace('\\', '/').split('/', 1)
        base_key = f"/{parts[0].lower()}"
        
        if base_key in DIRECTORIES:
            base_path = DIRECTORIES[base_key]
            if len(parts) > 1:
                return os.path.join(base_path, parts[1])
            return base_path
        return path_str # Assume it's already an absolute path if no key matched

    # --- HELPER: Security Verification ---
    def is_path_allowed(path_to_check):
        valid_base_dirs = list(set(DIRECTORIES.values()))
        # realpath resolves any "../../" tricks to ensure they stay in media folders
        real_path = os.path.realpath(path_to_check)
        return any(real_path.startswith(os.path.realpath(base)) for base in valid_base_dirs)

    # ==========================================
    # OPERATION: ls (LIST / TRAVERSE)
    # ==========================================
    if cmd == 'ls':
        if len(args) == 1:
            msg = "📂 **Base Directories:**\n\n"
            for key, path in DIRECTORIES.items():
                if key in ['/lmv', '/ltv', '/lmv2', '/ltv2']: continue # Skip link aliases
                msg += f"🔹 `{key[1:]}` ➡️ `{path}`\n"
            return await event.reply(msg)
            
        target_path = resolve_path(args[1])
        
        if not is_path_allowed(target_path):
            return await event.reply("⚠️ **Security Warning:** Path is outside allowed media directories.")
            
        if not os.path.exists(target_path):
            return await event.reply(f"❌ **Not Found:** `{target_path}`")
            
        if not os.path.isdir(target_path):
            size_str = format_bytes(os.path.getsize(target_path))
            return await event.reply(f"📄 **File:** `{target_path}`\n💾 **Size:** `{size_str}`")
            
        try:
            items = os.listdir(target_path)
            if not items:
                return await event.reply(f"📂 **{target_path}** is currently empty.")
                
            folders = sorted([i for i in items if os.path.isdir(os.path.join(target_path, i))])
            files = sorted([i for i in items if os.path.isfile(os.path.join(target_path, i))])
            
            msg = f"📂 **Path:** `{target_path}`\n\n"
            if folders:
                msg += "**Folders:**\n"
                for f in folders[:30]: msg += f"📁 `{f}`\n"
                if len(folders) > 30: msg += f"*...and {len(folders)-30} more.*\n"
                msg += "\n"
            if files:
                msg += "**Files:**\n"
                for f in files[:40]: msg += f"📄 `{f}`\n"
                if len(files) > 40: msg += f"*...and {len(files)-40} more.*\n"
                
            await event.reply(msg)
        except Exception as e:
            await event.reply(f"❌ **Error reading directory:** `{str(e)}`")

    # ==========================================
    # OPERATION: rn (RENAME)
    # ==========================================
    elif cmd == 'rn':
        if len(args) < 3:
            return await event.reply("❌ **Usage:** `/fm rn \"<path>\" \"<new_name>\"`")
            
        old_path = resolve_path(args[1])
        new_name = args[2]
        
        if not is_path_allowed(old_path):
             return await event.reply("⚠️ **Security Warning:** Path is outside allowed media directories.")
             
        if not os.path.exists(old_path):
             return await event.reply(f"❌ **Not Found:** `{old_path}`")
             
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            return await event.reply(f"❌ **Error:** Name `{new_name}` already exists in this location.")
            
        try:
            os.rename(old_path, new_path)
            await event.reply(f"✅ **Renamed!**\n📁 **From:** `{os.path.basename(old_path)}`\n🏷️ **To:** `{new_name}`\n📂 **New Path:** `{new_path}`")
        except Exception as e:
            await event.reply(f"❌ **Failed to rename:** `{str(e)}`")

    # ==========================================
    # OPERATION: rm (REMOVE / DELETE)
    # ==========================================
    elif cmd == 'rm':
        if len(args) < 2:
             return await event.reply("❌ **Usage:** `/fm rm \"<path>\"`")
             
        target_path = resolve_path(args[1])
        
        if not is_path_allowed(target_path):
             return await event.reply("⚠️ **Security Warning:** Path is outside allowed media directories.")
             
        if not os.path.exists(target_path):
             return await event.reply(f"❌ **Not Found:** `{target_path}`")
             
        try:
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            else:
                os.remove(target_path)
            await event.reply(f"🗑️ **Deleted:** `{target_path}`")
        except Exception as e:
            await event.reply(f"❌ **Failed to delete:** `{str(e)}`")
            
    else:
        await event.reply(f"❌ **Unknown operation:** `{cmd}`\nValid operations are `ls`, `rn`, and `rm`.")    


@bot.on(events.NewMessage(pattern=r'^/cancel$'))
async def cancel_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return
    
    if not event.is_reply:
        await event.reply("⚠️ Reply to a status message to cancel it.")
        return
    reply_msg = await event.get_reply_message()
    
    # Check for active download task
    task = active_downloads.get(reply_msg.id)
    if task:
        task.cancel()
        return await event.reply("🛑 Task Cancelled!")
        
    # Check for pending deletion confirmation
    if reply_msg.id in pending_deletions:
        del pending_deletions[reply_msg.id]
        return await event.reply("🛑 Deletion Operation Cancelled!")
        
    await event.reply("⚠️ No active task or pending operation found.")
        
        
@bot.on(events.NewMessage(pattern=r'^/del'))
async def delete_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return
    
    # --- SCENARIO 1: Replied to a message ---
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        
        # Check if replied to a Pending Bulk Deletion List
        if reply_msg.id in pending_deletions:
            paths_to_delete = pending_deletions[reply_msg.id]
            deleted_count = 0
            for path in paths_to_delete:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    elif os.path.exists(path):
                        os.remove(path)
                    deleted_count += 1
                except Exception as e:
                    print(f"Failed to delete {path}: {e}")
                    
            del pending_deletions[reply_msg.id] # Clean state
            return await event.reply(f"✅ **Successfully deleted {deleted_count} items.**")
            
        # Check if replied to a single completed download
        match = re.search(r'📂 \*\*Path:\*\* "(.*?)"', reply_msg.text)
        if match:
            filepath = match.group(1)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    filename = os.path.basename(filepath)
                    await event.reply(f"🗑️ **File deleted:** `{filename}`")
                except Exception as e:
                    await event.reply(f"❌ **Failed to delete:** `{str(e)}`")
            else:
                await event.reply(f"⚠️ **File not found at path:** `{filepath}`")
            return
            
        return await event.reply("❌ Invalid message replied to.")

    # --- SCENARIO 2: Standalone command with parameters (/del <dir> <keywords>) ---
    else:
        parts = event.text.strip().split(maxsplit=2)
        
        if len(parts) < 3:
            return await event.reply("❌ **Usage:** `/del mv <keyword.keyword2>`\n*(Or reply to a completed message)*")
            
        dir_key = f"/{parts[1].lower()}"
        raw_keywords = parts[2].lower()
        keywords = raw_keywords.split('.') # Split by dot
        
        target_dir = DIRECTORIES.get(dir_key)
        
        if not target_dir:
            return await event.reply("❌ **Invalid directory.** Available: `mv, tv, mv2, tv2`.")
            
        if not os.path.exists(target_dir):
            return await event.reply(f"❌ **Directory not found:** `{target_dir}`")

        matched_paths = []
        
        # Recursively search files and folders
        for root, dirs, files in os.walk(target_dir):
            # Check Folders
            for d in dirs:
                if all(k in d.lower() for k in keywords):
                    matched_paths.append(os.path.join(root, d))
            # Check Files
            for f in files:
                if all(k in f.lower() for k in keywords):
                    # Prevent listing a file if its parent folder is already slated for deletion
                    parent_matched = any(root.startswith(p) for p in matched_paths)
                    if not parent_matched:
                        matched_paths.append(os.path.join(root, f))
        
        if matched_paths:
            msg = f"⚠️ **Found {len(matched_paths)} match(es) for '{raw_keywords}':**\n\n"
            msg += "\n".join([f"`{p}`" for p in matched_paths[:15]])
            if len(matched_paths) > 15:
                msg += f"\n\n*...and {len(matched_paths) - 15} more.*"
                
            msg += "\n\n⚠️ **Reply to this message with `/del` to confirm, or `/cancel` to abort.**"
            
            sent_msg = await event.reply(msg)
            pending_deletions[sent_msg.id] = matched_paths # Save to state for confirmation
        else:
            await event.reply(f"⚠️ No matches found for all keywords `{raw_keywords}` in `{parts[1]}`.")


# 1. STANDARD HANDLER (/mv, /tv)
@bot.on(events.NewMessage(pattern=r'^/(mv|tv|mv2|tv2)$'))
async def standard_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return
    
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
@bot.on(events.NewMessage(pattern=r'^/l(mv|tv|mv2|tv2)'))
async def link_handler(event):
    """
    Handles links by asking the Userbot to fetch the message.
    """
    if event.sender_id not in ALLOWED_USERS:
        return
    
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
    
    
    