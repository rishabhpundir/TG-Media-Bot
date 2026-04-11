import re
import os
import time
import shlex
import shutil
import base64
import logging
import asyncio
import traceback

logger = logging.getLogger(__name__)

from aria_core import aria2_request, aria2_progress_tracker
from utils import format_bytes, sanitize_filename, ensure_mkv_extension
from config import (DIRECTORIES, ALLOWED_USERS, MAX_FILE_SIZE_BYTES, 
                    MAX_FILE_SIZE_GB, MAX_CONCURRENT_DOWNLOADS)
from state import (queue, active_downloads, pending_deletions, 
                   pending_aria_actions, current_concurrent_count)


bot = None
userbot = None


# --- HANDLERS ---
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

🧲 **Aria (`/aria`):**
`/aria <mv|tv|mv2|tv2> <link>` - Send Magnet/Direct link to Aria2c.
`/aria <mv|tv|mv2|tv2>` - Reply to a `.torrent` file to send to Aria2c.
`/aria list` - Show all active, waiting, and stopped Aria2 downloads.
`/aria <GID>` - Track the live status of a specific task.
`/aria start|stop|rm|del` - Reply to an Aria2 status msg to manage it.
  *(rm = Remove task, del = Remove task + Delete Files)*

🗄️ **File Manager (`/fm`):**
`/fm ls` - List base directories.
`/fm ls <dir_key/path>` - View folder contents (e.g., `/fm ls tv/Breaking Bad`).
`/fm rn "<path>" "<new_name>"` - Rename a file/folder.
`/fm rm "<path>"` - Instantly delete a file/folder.

⚙️ **Task Management:**
`/cancel` - Reply to an active download or pending list to abort.
`/del` - Reply to a "Download Complete" message to delete that file.
`/del <mv|tv|mv2|tv2> <keyword1.keyword2>` - Search for and safely delete files matching keywords.
"""

    await event.reply(welcome_text)


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


async def aria_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    dir_key = f"/{event.pattern_match.group(1).lower()}"
    target_dir = DIRECTORIES.get(dir_key)
    link = event.pattern_match.group(2)
    
    is_reply = event.is_reply
    reply_msg = await event.get_reply_message() if is_reply else None

    # Determine the payload
    gid = None
    filename_display = "Unknown Task"

    status_msg = await event.reply("🔄 Sending task to Aria2...")

    try:
        # SCENARIO 1: Replied to a .torrent file
        if is_reply and reply_msg.media and reply_msg.file.ext == ".torrent":
            await status_msg.edit("📥 Downloading .torrent file locally...")
            torrent_path = await reply_msg.download_media()
            
            with open(torrent_path, "rb") as f:
                b64_torrent = base64.b64encode(f.read()).decode("utf-8")
                
            os.remove(torrent_path) # Clean up local .torrent file
            
            filename_display = reply_msg.file.name
            await status_msg.edit("🚀 Pushing torrent to Aria2...")
            
            # Send to Aria2 RPC
            options = {"dir": target_dir}
            gid = await aria2_request("addTorrent", [b64_torrent, [], options])

        # SCENARIO 2: Provided a Magnet Link or Direct URL
        elif link:
            link = link.strip()
            filename_display = "Magnet Link / URL Task"
            options = {"dir": target_dir}
            gid = await aria2_request("addUri", [[link], options])
            
        else:
            return await status_msg.edit("❌ **Usage Error:** Provide a link or reply to a `.torrent` file.\nExample: `/aria mv <magnet_link>`")

        # Start background tracker for this download
        bot.loop.create_task(aria2_progress_tracker(gid, status_msg, filename_display))

    except Exception as e:
        await status_msg.edit(f"❌ **Aria2 Error:** `{str(e)}`\nMake sure the Aria2 service is running and configured correctly.")


async def aria_manage_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    cmd = event.pattern_match.group(1).lower()

    # --- OPERATION: LIST ---
    if cmd == 'list':
        try:
            active = await aria2_request("tellActive")
            waiting = await aria2_request("tellWaiting", [0, 10])
            stopped = await aria2_request("tellStopped", [0, 10])

            msg = "📊 **Aria2c Downloads**\n\n"

            def format_task(task, status_icon):
                gid = task.get("gid")
                name = "Unknown Metadata/Task"
                if "bittorrent" in task and "info" in task["bittorrent"]:
                    name = task["bittorrent"]["info"].get("name", name)
                elif task.get("files") and task["files"][0].get("path"):
                    name = os.path.basename(task["files"][0]["path"])
                
                total = int(task.get("totalLength", 0))
                completed = int(task.get("completedLength", 0))
                perc = (completed * 100 / total) if total > 0 else 0
                return f"{status_icon} `{name}`\n🆔 **GID:** `{gid}`\n📊 `{perc:.1f}%` | 💾 `{format_bytes(completed)}/{format_bytes(total)}`\n\n"

            msg += "**Active:**\n" + ("".join([format_task(t, "▶️") for t in active]) if active else "None\n\n")
            msg += "**Waiting:**\n" + ("".join([format_task(t, "⏳") for t in waiting]) if waiting else "None\n\n")
            msg += "**Stopped/Completed:**\n" + ("".join([format_task(t, "⏹️") for t in stopped]) if stopped else "None\n\n")
            
            if len(msg) > 4000: msg = msg[:4000] + "...\n*(Truncated)*"
            await event.reply(msg)
        except Exception as e:
            await event.reply(f"❌ **Failed to fetch list:** `{e}`\n\n`{traceback.format_exc()}`")
        return

    # --- OPERATION: START, STOP, RM, DEL ---
    if not event.is_reply:
        return await event.reply("❌ **Usage:** Reply to an Aria2 status message or an item from `/aria list`.")
    
    reply_msg = await event.get_reply_message()
    match = re.search(r'GID:\*\* `([a-fA-F0-9]+)`', reply_msg.text)
    if not match:
        return await event.reply("❌ Could not find a valid **GID** in the replied message.")
        
    gid = match.group(1)

    try:
        if cmd == 'start':
            await aria2_request("unpause", [gid])
            await event.reply(f"▶️ **Resumed task:** `{gid}`")
            
        elif cmd == 'stop':
            await aria2_request("forcePause", [gid])
            await event.reply(f"⏸️ **Paused task:** `{gid}`")
            
        elif cmd in ['rm', 'del']:
            action_type = "remove from Aria2c ONLY (Files kept)" if cmd == 'rm' else "remove from Aria2c AND DELETE all downloaded files permanently"
            
            sent_msg = await event.reply(
                f"⚠️ **Confirmation Required**\n\n"
                f"You are about to **{action_type}** for task:\n🆔 `{gid}`\n\n"
                f"Reply to this message with `/del` to confirm, or `/cancel` to abort."
            )
            pending_aria_actions[sent_msg.id] = {"action": cmd, "gid": gid}

    except Exception as e:
        await event.reply(f"❌ **Action failed:** `{str(e)}`")


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
    
    # Check for pending Aria deletion confirmation
    if reply_msg.id in pending_aria_actions:
        del pending_aria_actions[reply_msg.id]
        return await event.reply("🛑 Aria2 Deletion Cancelled!")
        
    await event.reply("⚠️ No active task or pending operation found.")
        
        
async def delete_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return
    
    # --- SCENARIO 1: Replied to a message ---
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        
    # Check if replied to a Pending Aria Action
        if reply_msg.id in pending_aria_actions:
            action_data = pending_aria_actions.pop(reply_msg.id)
            action, gid = action_data["action"], action_data["gid"]
            
            try:
                if action == 'del':
                    # Extract files BEFORE removing the task
                    files_info = []
                    try: files_info = await aria2_request("getFiles", [gid])
                    except: pass
                        
                    # 1. Force remove from Aria2 to release file locks
                    try: await aria2_request("forceRemove", [gid])
                    except: 
                        try: await aria2_request("removeDownloadResult", [gid])
                        except: pass

                    # 2. Delete files from storage
                    deleted_count = 0
                    for f in files_info:
                        path = f.get("path")
                        if path and os.path.exists(path):
                            try:
                                os.remove(path)
                                deleted_count += 1
                                os.rmdir(os.path.dirname(path)) # Clean up parent dir if empty
                            except: pass
                            
                    return await event.reply(f"🗑️ **Task Removed:** `{gid}`\n🧹 **Deleted {deleted_count} files/folders.**")
                    
                else: # rm logic
                    try: await aria2_request("forceRemove", [gid])
                    except: 
                        try: await aria2_request("removeDownloadResult", [gid])
                        except: pass
                    return await event.reply(f"🗑️ **Task Removed:** `{gid}`\n*(Files were kept in storage)*")
                    
            except Exception as e:
                return await event.reply(f"❌ **Failed to remove task:** `{str(e)}`")
        
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
                    logger.exception(f"Failed to delete {path}: {e}")
                    
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


async def aria_track_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    gid = event.pattern_match.group(1)
    status_msg = await event.reply(f"🔄 Fetching status for GID: `{gid}`...")

    try:
        # First, ping Aria2c to ensure the task exists and to extract its name
        status = await aria2_request("tellStatus", [gid])
        
        filename_display = "Unknown Task"
        if "bittorrent" in status and "info" in status["bittorrent"]:
            filename_display = status["bittorrent"]["info"].get("name", filename_display)
        elif status.get("files") and status["files"][0].get("path"):
            filename_display = os.path.basename(status["files"][0]["path"])
            
        # Spawn the existing tracker! 
        # It automatically handles looping if active, or breaks immediately if paused/completed
        asyncio.create_task(aria2_progress_tracker(gid, status_msg, filename_display))

    except Exception as e:
        logger.exception(f"Aria2 Track Error for GID {gid}: {e}")
        await status_msg.edit(f"❌ **Failed to fetch task:** `{gid}`\n`{str(e)}`")
        
        
        