import re
import os
import time
import json
import shlex
import shutil
import base64
import logging
import asyncio
import traceback
import subprocess

logger = logging.getLogger(__name__)

import video.yt_dl as ytdl
from core.aria_core import aria2_request, aria2_progress_tracker
from core.utils import format_bytes, sanitize_filename, ensure_mkv_extension
from config import (DIRECTORIES, ALLOWED_USERS, MAX_FILE_SIZE_BYTES, 
                    MAX_FILE_SIZE_GB, MAX_CONCURRENT_DOWNLOADS)
from state import (queue, active_downloads, pending_deletions, 
                   pending_aria_actions, current_concurrent_count)
from gdrive.gdriveup import upload_single_target
from gdrive.gd_stream import stream_url_to_drive
from telethon.tl.types import DocumentAttributeVideo


bot = None
userbot = None
active_gd_uploads = {}


# --- HANDLERS ---
async def start_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return
        
    welcome_text = """👋 **Welcome back, Rishabh! System is online and ready.**

Here is your current command list:

📥 **Downloads:**
`/mv` / `/mv2` - Reply to file -> Save to Movies.
`/tv` / `/tv2` - Reply to file -> Save to TV.
`/docu` - Reply to file -> Save to Documentaries.
`/lmv <link>` / `/lmv2 <link>` - Fetch restricted link -> Movies.
`/ltv <link>` / `/ltv2 <link>` - Fetch restricted link -> TV.
`/search <Channel_ID> (<keywords>)` - Search channel and get JSON file list.
*(Reply to the JSON list with `/lmv <Folder_Name>` to bulk download them)*

🧲 **Aria (`/aria`):**
`/aria <mv|tv|mv2|tv2|docu> <link>` - Send Magnet/Direct link to Aria2c.
`/aria <mv|tv|mv2|tv2|docu>` - Reply to a `.torrent` file to send to Aria2c.

📺 **YouTube-DL (`/ytdl`):**
`/ytdl <dir_key> <link>` - Download video to folder.
`/ytdl tg <dir_key> <link>` - Download & upload to TG Channel.
*(Reply to a manifest .txt file to batch process)*

☁️ **Google Drive Upload (`/gd`):**
`/gd` - Reply to a completed download message to upload it.
`/gd "<dir_key>/<name>"` - Directly upload a file/folder (e.g., `/gd "tv/Breaking Bad"`).
`/gd x <link> [filename.ext]` - Stream a direct link straight to Drive (never touches disk).

🗄️ **File Manager (`/fm`):**
`/fm ls` - List base directories.
`/fm ls <dir_key/path>` - View contents.
`/fm rn "<path>" "<new_name>"` - Rename.
`/fm rn all "<dir>" "<pattern>"` - Bulk rename alphabetically.
`/fm mov "<src>" "<dest>"` - Move files.
`/fm rm "<path>"` - Delete file/folder.

⚙️ **Miscellaneous:**
`/cancel` - Abort active task.
`/del` - Delete completed file (reply).
`/del <dir_key> <keywords>` - Search & safely delete.
`/cmd <category>` - Detailed help.
"""

    await event.reply(welcome_text)


async def fm_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    args_str = event.pattern_match.group(1)
    
    help_text = """🗄️ **File Manager (`/fm`)**
**Usage:**
`/fm ls` - List base directories (e.g., `/fm ls`)
`/fm ls <dir>` - View folder contents (e.g., `/fm ls tv/Breaking Bad`)
`/fm rn "<path>" "<new>"` - Rename a file/folder (e.g., `/fm rn "tv/Old.mkv" "New.mkv"`)
`/fm rn all "<dir>" "<pattern>"` - Bulk rename alphabetically (e.g., `/fm rn all "tv/Show" "E{NUM:1}.mkv"`)
`/fm mov "<src>" "<dest>"` - Move a file/folder (e.g., `/fm mov "mv/File.mkv" "tv/Show/"`)
`/fm rm "<path>"` - Delete a file/folder (e.g., `/fm rm "tv/Bad.mkv"`)

*Tip: You can use directory keys as shortcuts!*"""

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
        # --- BULK RENAME (rn all) ---
        if len(args) > 1 and args[1].lower() == 'all':
            if len(args) < 4:
                return await event.reply("❌ **Usage:** `/fm rn all \"<dir>\" \"<pattern>\"`\nExample: `/fm rn all \"tv/Show\" \"Ep_{NUM:1}.mkv\"`")
                
            target_dir = resolve_path(args[2])
            pattern = args[3]
            
            if not is_path_allowed(target_dir):
                return await event.reply("⚠️ **Security Warning:** Path is outside allowed media directories.")
                
            if not os.path.isdir(target_dir):
                return await event.reply(f"❌ **Not a directory:** `{target_dir}`")
                
            files = sorted([f for f in os.listdir(target_dir) if os.path.isfile(os.path.join(target_dir, f))])
            if not files:
                return await event.reply(f"⚠️ **Directory is empty:** `{target_dir}`")
                
            renamed_count = 0
            errors = []
            
            for i, old_file in enumerate(files):
                # Dynamically replace all {NUM:X} with (X + i)
                def repl(match):
                    start_val = int(match.group(1))
                    return str(start_val + i)
                    
                new_filename = re.sub(r'\{NUM:(\d+)\}', repl, pattern)
                
                old_path = os.path.join(target_dir, old_file)
                new_path = os.path.join(target_dir, new_filename)
                
                if os.path.exists(new_path) and old_path != new_path:
                    errors.append(f"`{old_file}` -> Skipped (Name `{new_filename}` already exists)")
                    continue
                    
                try:
                    os.rename(old_path, new_path)
                    renamed_count += 1
                except Exception as e:
                    errors.append(f"`{old_file}` -> Error: {str(e)}")
                    
            msg = f"✅ **Bulk Rename Complete!**\nSuccessfully renamed `{renamed_count}` out of `{len(files)}` files."
            if errors:
                msg += "\n\n⚠️ **Issues:**\n" + "\n".join(errors[:10])
                if len(errors) > 10: msg += f"\n*...and {len(errors)-10} more.*"
                
            return await event.reply(msg)

        # --- SINGLE RENAME ---
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
    # ==========================================
    # OPERATION: mv (MOVE)
    # ==========================================
    elif cmd == 'mov':
        if len(args) < 3:
            return await event.reply("❌ **Usage:** `/fm mov \"<src_path>\" \"<dest_path>\"`")
            
        src_path = resolve_path(args[1])
        dest_path = resolve_path(args[2])
        
        if not is_path_allowed(src_path) or not is_path_allowed(dest_path):
             return await event.reply("⚠️ **Security Warning:** Path is outside allowed media directories.")
             
        if not os.path.exists(src_path):
             return await event.reply(f"❌ **Not Found:** `{src_path}`")
             
        # Determine parent destination folder
        parent_dest = os.path.dirname(dest_path) if not os.path.isdir(dest_path) else dest_path
        if not os.path.exists(parent_dest):
             return await event.reply(f"❌ **Destination parent folder does not exist:** `{parent_dest}`")
             
        try:
            shutil.move(src_path, dest_path)
            await event.reply(f"✅ **Moved!**\n📦 **From:** `{src_path}`\n🛬 **To:** `{dest_path}`")
        except Exception as e:
            await event.reply(f"❌ **Failed to move:** `{str(e)}`")
            
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
        
        # Nuke the pending queue
        cleared_count = 0
        try:
            # Flushes the asyncio.Queue instantly without blocking the event loop
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
                cleared_count += 1
        except Exception as e:
            # Assuming logger is defined, otherwise you can use print(f"Error: {e}")
            logger.error(f"Error clearing queue: {e}")
            
        return await event.reply(
            f"🛑 **Active Task Cancelled!**\n"
            f"🗑️ **Cleared Queue:** `{cleared_count}` pending files removed."
        )
        
    # Check for active Google Drive upload
    if reply_msg.id in active_gd_uploads:
        active_gd_uploads[reply_msg.id]["cancelled"] = True
        return await event.reply("🛑 Signalling Drive Upload to abort...")
        
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


async def link_handler(event):
    """
    Handles single links or batch JSON payloads by asking the Userbot to fetch the messages.
    """
    if event.sender_id not in ALLOWED_USERS:
        return
    
    text_split = event.text.strip().split(maxsplit=1)
    cmd_base = text_split[0].lower().replace('/l', '/') # Translates /lmv -> /mv for dir lookup

    # BATCH JSON INTERCEPTOR
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        json_str = None
        
        # --- NEW: Support reading directly from a replied .json file ---
        if reply_msg.document and getattr(reply_msg.file, 'name', '').endswith('.json'):
            try:
                status = await event.reply("📄 Reading JSON file...")
                # Download file directly into memory as bytes, decode to string
                file_bytes = await reply_msg.download_media(file=bytes)
                json_str = file_bytes.decode('utf-8')
                await status.delete()
            except Exception as e:
                return await event.reply(f"❌ Failed to read JSON file: `{e}`")
                
        # --- ORIGINAL: Support reading JSON from message text ---
        elif reply_msg.text and '{' in reply_msg.text and '}' in reply_msg.text:
            json_str = reply_msg.text

        if json_str:
            try:
                # Mobile keyboards often replace standard quotes with smart quotes, breaking JSON
                json_str = json_str.replace('“', '"').replace('”', '"')
                
                # Extract actual JSON from markdown code blocks if present
                if '```json' in json_str:
                    json_str = json_str.split('```json')[1].split('```')[0]
                elif '```' in json_str:
                    json_str = json_str.split('```')[1].split('```')[0]
                    
                batch_data = json.loads(json_str)
                
                # We have a valid JSON batch! Setup the destination.
                folder_name = text_split[1].strip() if len(text_split) > 1 else f"Batch_{int(time.time())}"
                base_target_dir = DIRECTORIES.get(cmd_base)
                
                if not base_target_dir:
                    return await event.reply("❌ Invalid base directory command.")
                    
                # Create the requested sub-directory
                target_dir = os.path.join(base_target_dir, folder_name)
                os.makedirs(target_dir, exist_ok=True) 
                
                status = await event.reply(f"🕵️ **Userbot:** Processing batch of `{len(batch_data)}` files into folder `{folder_name}`...")
                queued_count = 0
                
                for custom_name, link in batch_data.items():
                    # Parse the standard t.me format
                    match = re.search(r't\.me/(?:c/)?(\w+|\d+)/(\d+)', link)
                    if not match: continue
                    
                    identifier = match.group(1)
                    msg_id = int(match.group(2))
                    entity = int(f"-100{identifier}") if identifier.isdigit() else identifier
                    
                    try:
                        restricted_msg = await userbot.get_messages(entity, ids=msg_id)
                        if not restricted_msg or not restricted_msg.media: continue
                        if restricted_msg.file.size > MAX_FILE_SIZE_BYTES: continue
                        
                        clean_name = ensure_mkv_extension(sanitize_filename(custom_name))
                        
                        position = queue.qsize() + 1
                        await queue.put((event, restricted_msg, target_dir, clean_name))
                        queued_count += 1
                    except Exception as e:
                        logger.error(f"Batch fetch failed for {link}: {e}")
                        
                await status.edit(f"⏳ **Queued {queued_count} files for batch download!**\n📂 **Destination:** `{target_dir}`")
                return # Exit successfully!

            except json.JSONDecodeError:
                # If parsing fails, fall through to normal single-link logic silently
                pass

    # STANDARD SINGLE LINK LOGIC
    if len(text_split) < 2:
        return await event.reply("❌ Usage: `/lmv <link>` or `/ltv <link>`\n*(Or reply to a JSON list with `/lmv <Folder Name>`)*")

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
        
        
async def unzip_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    is_del = bool(event.pattern_match.group(1))
    dir_key = event.pattern_match.group(2)
    keywords_str = event.pattern_match.group(3)

    filepath = None

    # SCENARIO 1: Standalone keyword search (/unzip del mv keyword.here)
    if dir_key and keywords_str:
        target_dir = DIRECTORIES.get(f"/{dir_key.lower()}")
        keywords = keywords_str.lower().split('.')

        found_paths = []
        for root, dirs, files in os.walk(target_dir):
            for f in files:
                if all(k in f.lower() for k in keywords):
                    if f.lower().endswith(('.zip', '.rar', '.tar', '.gz', '.bz2', '.xz', '.7z')):
                        found_paths.append(os.path.join(root, f))

        if not found_paths:
            return await event.reply(f"⚠️ No compressed files found matching `{keywords_str}` in `{dir_key}`.")

        filepath = found_paths[0] # Act on the first match

    # SCENARIO 2: Reply to a message
    elif event.is_reply:
        reply_msg = await event.get_reply_message()
        
        # Parse standard Aria and Downloader output layouts
        path_match = re.search(r'📂 \*\*Path:\*\* `([^`]+)`', reply_msg.text)
        name_match = re.search(r'🏷️ \*\*Name:\*\* `([^`]+)`', reply_msg.text)
        alt_path_match = re.search(r'📂 \*\*Path:\*\* "(.*?)"', reply_msg.text)

        if path_match and name_match:
            filepath = os.path.join(path_match.group(1), name_match.group(1))
        elif path_match:
            filepath = path_match.group(1) 
        elif alt_path_match:
            filepath = alt_path_match.group(1)
        else:
            # Fallback: look for any absolute Linux path in the text
            gen_match = re.search(r'(/[\w\.\-\/]+(?:zip|rar|tar|gz|bz2|xz|7z))', reply_msg.text, re.IGNORECASE)
            if gen_match:
                filepath = gen_match.group(1)

        if not filepath or not os.path.exists(filepath):
             return await event.reply("❌ Could not extract a valid, existing archive file path from the replied message.")
    else:
         return await event.reply("❌ **Usage:** Reply to a message with `/unzip [del]` or use `/unzip [del] <dir> <keywords>`")

    if not os.path.isfile(filepath):
         return await event.reply(f"❌ Target is not a file: `{filepath}`")

    filename = os.path.basename(filepath)
    status_msg = await event.reply(f"🔄 **Extracting:** `{filename}`...")

    # Setup extraction directory (strips extension to create a folder with the same name)
    extract_dir = os.path.splitext(filepath)[0] 
    os.makedirs(extract_dir, exist_ok=True)

    try:
        # Try native Python extraction first (zip, tar, gzip, etc.)
        try:
            shutil.unpack_archive(filepath, extract_dir)
        except shutil.ReadError:
            # Fallback to system subprocess for formats Python natively misses (rar, 7z)
            if filepath.lower().endswith('.rar'):
                subprocess.run(['unrar', 'x', '-y', filepath, f"{extract_dir}/"], check=True, capture_output=True)
            elif filepath.lower().endswith('.7z'):
                subprocess.run(['7z', 'x', f'-o{extract_dir}', '-y', filepath], check=True, capture_output=True)
            else:
                raise Exception("Format not supported by standard Python tools or system fallback.")

        # Extraction Successful -> Build the file list
        extracted_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                extracted_files.append(f)

        file_list_str = "\n".join([f"📄 `{f}`" for f in extracted_files[:15]])
        if len(extracted_files) > 15:
            file_list_str += f"\n*...and {len(extracted_files) - 15} more.*"

        success_text = f"✅ **Successfully Extracted!**\n📂 **Folder:** `{os.path.basename(extract_dir)}`\n\n**Contents:**\n{file_list_str}"

        # Handle the 'del' flag
        if is_del:
            try:
                os.remove(filepath)
                success_text += f"\n\n🗑️ **Original archive deleted:** `{filename}`"
            except Exception as del_err:
                success_text += f"\n\n⚠️ **Warning:** Failed to delete original archive: `{str(del_err)}`"

        await status_msg.edit(success_text)

    except subprocess.CalledProcessError as sub_e:
        # Clean up empty folder on fail
        shutil.rmtree(extract_dir, ignore_errors=True) 
        err_msg = sub_e.stderr.decode().strip() if sub_e.stderr else str(sub_e)
        await status_msg.edit(f"❌ **Extraction failed (System Error):**\n`{err_msg}`\n\n*(Note: Make sure `unrar` and `p7zip-full` are installed on your Pi)*")
        
    except Exception as e:
        shutil.rmtree(extract_dir, ignore_errors=True)
        await status_msg.edit(f"❌ **Extraction failed:** `{str(e)}`")
        

async def cls_handler(event):
    """Deletes all non-pinned messages in the current chat using the Userbot."""
    if event.sender_id not in ALLOWED_USERS:
        return
        
    status = await event.reply("🧹 **Sweeping chat...**")
    message_ids = []
    
    try:
        # Determine the correct chat ID for the userbot to look at.
        # If you are talking to the bot in a private chat, event.chat_id is YOUR user ID.
        # But the userbot needs to look at its chat with the BOT.
        target_chat = event.chat_id
        if event.is_private:
            bot_info = await bot.get_me()
            target_chat = bot_info.id
            
        # Fetch history using the Userbot (which has full GetHistory access)
        async for msg in userbot.iter_messages(target_chat):
            if not msg.pinned:
                message_ids.append(msg.id)
        
        # Delete in chunks of 100 (Telegram API limit) using the Userbot
        if message_ids:
            for i in range(0, len(message_ids), 100):
                # revoke=True ensures the messages are deleted for both you and the bot
                await userbot.delete_messages(target_chat, message_ids[i:i+100], revoke=True)
                
        # (We don't need to manually delete the 'status' message because 
        # the userbot will have already grabbed it in the history sweep and wiped it!)
        
    except Exception as e:
        logger.exception("Error clearing messages")
        try:
            # Only try to edit the status if it somehow survived the sweep
            await status.edit(f"❌ **Failed to clear chat:** `{str(e)}`")
        except:
            pass
        

async def cmd_handler(event):
    """Provides detailed command examples based on the requested module."""
    if event.sender_id not in ALLOWED_USERS:
        return
        
    module = event.pattern_match.group(1)
    if not module:
        return await event.reply("❌ **Usage:** `/cmd <module>`\nAvailable modules: `tgdl`, `aria`, `ytdl`, `gd`, `unzip`, `fm`, `misc`")
        
    module = module.strip().lower()
    
    help_texts = {
        "tgdl": "📥 **Downloads (`tgdl`)**\n\n"
                "`/mv` / `/mv2` / `/tv` / `/tv2` / `/docu` - Save media to respective folder.\n*Example:* Reply to a `.mkv` file with `/docu`\n\n"
                "`/lmv <link>` / `/ltv <link>` - Fetch restricted links.\n*Example:* `/lmv https://t.me/c/123/456`\n\n"
                "🔍 **Bulk Search & Download:**\n"
                "`/search <Channel_ID> (<keywords>)` - Get a JSON list of files matching keywords.\n*Example:* `/search -10012345678 (spider man)`\n\n"
                "*(Reply to the JSON list with `/lmv <Folder_Name>` to bulk download!)*",
        
        "aria": "🧲 **Aria (`aria`)**\n\n"
                "`/aria <mv|tv|mv2|tv2|docu> <link>` - Send link to Aria2c.\n*Example:* `/aria mv magnet:?xt=urn:btih:...`\n\n"
                "`/aria <mv|tv|mv2|tv2|docu>` - Send `.torrent` to Aria2c.\n*Example:* Reply to a `.torrent` file with `/aria tv`\n\n"
                "`/aria list` - Show all downloads.\n*Example:* `/aria list`\n\n"
                "`/aria <GID>` - Track specific status.\n*Example:* `/aria 1a2b3c4d5e6f7g8h`\n\n"
                "`/aria start|stop|rm|del` - Manage task.\n*Example:* Reply to a tracking message with `/aria stop`",
                
        "ytdl": "📺 **YouTube-DL (`ytdl`)**\n\n"
                "`/ytdl <dir> <url>` - Download streams/videos.\n*Example:* `/ytdl docu https://youtube.com/...`\n\n"
                "`/ytdl tg <dir> <url>` - Download & upload to destination channel.\n*Example:* `/ytdl tg tv https://...`\n\n"
                "*(Supports replying to a .txt manifest for batch queuing)*",
                
        "gd":   "☁️ **Google Drive (`gd`)**\n\n"
                "`/gd` - Upload a completed download to Drive.\n*Example:* Reply to a download success message with `/gd`\n\n"
                "`/gd \"<dir_key>/<name>\"` - Directly upload a specific file/folder.\n*Example:* `/gd \"tv/Breaking Bad\"`\n\n"
                "`/gd x <link> [filename.ext]` - Stream a direct download link straight to Drive without saving to disk.\n"
                "*Example:* `/gd x https://host/file.mkv`\n"
                "*(Optional filename forces a name when the link can't supply one. Best for clean direct links; use `/aria` for cookie/login-gated ones.)*",

        "unzip": "🗜️ **Archive Management (`unzip`)**\n\n"
                 "`/unzip` - Extract archive in place.\n*Example:* Reply to completed download with `/unzip`\n\n"
                 "`/unzip del` - Extract & delete original.\n*Example:* Reply to download with `/unzip del`\n\n"
                 "`/unzip <dir> <keywords>` - Search & extract.\n*Example:* `/unzip mv inception.2010`\n\n"
                 "`/unzip del <dir> <keywords>` - Search, extract & delete.\n*Example:* `/unzip del tv breaking.bad.s01`",
                 
        "fm": "🗄️ **File Manager (`fm`)**\n\n"
              "`/fm ls` - List base directories.\n*Example:* `/fm ls`\n\n"
              "`/fm ls <dir_key/path>` - View contents.\n*Example:* `/fm ls tv/Breaking Bad`\n\n"
              "`/fm rn \"<path>\" \"<new_name>\"` - Rename file/folder.\n*Example:* `/fm rn \"tv/old.mkv\" \"new.mkv\"`\n\n"
              "`/fm rn all \"<dir>\" \"<pattern>\"` - Bulk rename alphabetically.\n*Example:* `/fm rn all \"tv/Show\" \"S0{NUM:1} E0{NUM:7}.mkv\"`\n\n"
              "`/fm mov \"<src>\" \"<dest>\"` - Move file/folder.\n*Example:* `/fm mov \"mv/File.mkv\" \"tv/Show/\"`\n\n"
              "`/fm rm \"<path>\"` - Delete file/folder.\n*Example:* `/fm rm \"tv/BadFile.mkv\"`",
              
        "misc": "⚙️ **Miscellaneous (`misc`)**\n\n"
                "`/cancel` - Abort active/pending task.\n*Example:* Reply to progress with `/cancel`\n\n"
                "`/del` - Delete completed file.\n*Example:* Reply to completion with `/del`\n\n"
                "`/del <dir> <keywords>` - Search & safely delete.\n*Example:* `/del mv sample.video`\n\n"
                "`/cls` - Clear all non-pinned messages.\n*Example:* `/cls`\n\n"
                "`/cmd <module>` - Show specific help.\n*Example:* `/cmd aria`"
    }
    
    if module in help_texts:
        await event.reply(help_texts[module])
    else:
        await event.reply(f"❌ **Unknown module:** `{module}`\nAvailable modules: `tgdl`, `aria`, `ytdl`, `gd`, `unzip`, `fm`, `misc`")
        
        
async def _gd_stream_url(event, url, filename=None, username=None, password=None):
    """Direct streaming pass-through: URL -> Google Drive, nothing touches disk."""
    target_name = filename or url.split("/")[-1].split("?")[0] or "download"
    status_msg = await event.reply(
        f"🌊 **Streaming to Google Drive (no disk):**\n`{target_name}`\n\n*(Starting rclone...)*"
    )

    start_time = time.time()
    state = {"body": None, "pct": None}

    def fmt_elapsed():
        secs = int(time.time() - start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m{s:02d}s"
        return f"{m}m{s:02d}s" if m else f"{s}s"

    def render():
        elapsed = fmt_elapsed()
        if state["body"] is None:
            return (f"🌊 **Streaming to Drive:** `{target_name}`\n"
                    f"⏳ Connecting & transferring... (elapsed `{elapsed}`)")
        if state["pct"] is None:
            head = "🌊 *transferring (total size unknown)*"
        else:
            blocks = state["pct"] // 10
            head = "🟦" * blocks + "⬜" * (10 - blocks) + f" **{state['pct']}%**"
        return (f"🌊 **Streaming to Drive:** `{target_name}`\n"
                f"{head}\n"
                f"⏳ elapsed `{elapsed}`\n"
                f"`{state['body']}`")

    async def stream_progress(pct, body):
        # Just record the latest stats; the heartbeat task does the editing.
        state["body"] = body
        state["pct"] = pct

    async def heartbeat():
        # Edits on a fixed 5s cadence regardless of rclone's output, so elapsed
        # time always ticks even before/without any "Transferred:" line.
        while True:
            await asyncio.sleep(10)
            try:
                await status_msg.edit(render())
            except Exception:
                pass  # ignore "not modified" / transient flood errors

    global active_gd_uploads
    cancel_flag = {"cancelled": False}
    active_gd_uploads[status_msg.id] = cancel_flag

    hb_task = asyncio.create_task(heartbeat())
    try:
        await stream_url_to_drive(url, stream_progress, cancel_flag, 
                                  filename=filename, username=username, 
                                  password=password)
        await status_msg.edit(
            f"✅ **Streamed to Google Drive!**\n☁️ `{target_name}`\n"
            f"⏳ took `{fmt_elapsed()}`"
        )
    except Exception as e:
        if cancel_flag.get("cancelled"):
            await status_msg.edit(f"🛑 **Stream Cancelled:**\n`{target_name}`")
            logger.info("GD stream cancelled: %s", url)
        else:
            logger.exception("GD stream error: %s", e)
            await status_msg.edit(f"❌ **Stream Failed:**\n`{str(e)}`")
    finally:
        hb_task.cancel()
        if status_msg.id in active_gd_uploads:
            del active_gd_uploads[status_msg.id]
        
        
async def gd_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    args_str = event.pattern_match.group(1)
    filepath = None

    # --- HELPER: Resolve Shortcut Paths ---
    def resolve_path(path_str):
        parts = path_str.replace('\\', '/').split('/', 1)
        base_key = f"/{parts[0].lower()}"
        if base_key in DIRECTORIES:
            base_path = DIRECTORIES[base_key]
            if len(parts) > 1:
                return os.path.join(base_path, parts[1])
            return base_path
        return path_str 

    # SCENARIO 1: Standalone command with path (/gd "tv/Show Name")
    if args_str:
        try:
            args = shlex.split(args_str)
        except ValueError as e:
            return await event.reply(f"❌ **Parse Error:** Check your quotes.\n`{str(e)}`")

        # --- streaming pass-through:  /gd x <direct download link> [filename] [-u username] [-p password] ---
        if args and args[0].lower() == 'x':
            if len(args) < 2:
                return await event.reply("❌ **Usage:** `/gd x <direct download link> [filename.ext] [-u user] [-p pass]`")
            
            url = args[1]
            opt_name = None
            username = None
            password = None
            
            i = 2
            while i < len(args):
                if args[i] == "-u" and i + 1 < len(args):
                    username = args[i+1]
                    i += 2
                elif args[i] == "-p" and i + 1 < len(args):
                    password = args[i+1]
                    i += 2
                elif opt_name is None and not args[i].startswith("-"):
                    opt_name = args[i]
                    i += 1
                else:
                    i += 1

            return await _gd_stream_url(event, url, filename=opt_name, username=username, password=password)

        if args:
            filepath = resolve_path(args[0])

    # SCENARIO 2: Reply to a completed download message
    elif event.is_reply:
        reply_msg = await event.get_reply_message()
        
        # This matches the output layout from Telegram, Links, Aria, and Unzip
        path_match = re.search(r'📂 \*\*Path:\*\* `([^`]+)`', reply_msg.text)
        name_match = re.search(r'🏷️ \*\*Name:\*\* `([^`]+)`', reply_msg.text)
        alt_path_match = re.search(r'📂 \*\*Path:\*\* "(.*?)"', reply_msg.text)

        if path_match and name_match:
            filepath = os.path.join(path_match.group(1), name_match.group(1))
        elif path_match:
            filepath = path_match.group(1) 
        elif alt_path_match:
            filepath = alt_path_match.group(1)
        else:
            return await event.reply("❌ Could not extract a valid path from the replied message. Make sure it's a 'Download Complete' message.")
            
    else:
        return await event.reply("❌ **Usage:** Reply to a completed download with `/gd` OR use `/gd \"<dir_key>/<file_or_folder_name>\"`")

    # Verification & Execution
    if not filepath or not os.path.exists(filepath):
        return await event.reply(f"❌ **File or Folder not found on disk:**\n`{filepath}`")

    target_name = os.path.basename(filepath)
    status_msg = await event.reply(f"☁️ **Uploading to Google Drive:**\n`{target_name}`...\n\n*(Calculating...)*")

    # --- NEW: Thread-safe Progress Tracking ---
    last_update_time = [0]
    start_time = [time.time()]
    current_file_tracker = [""]
    
    # Explicitly grab the main thread's asyncio loop before entering the background thread
    main_loop = asyncio.get_running_loop()

    async def drive_progress_async(current, total, current_file_name):
        elapsed_time = time.time() - start_time[0]
        speed = current / elapsed_time if elapsed_time > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        percentage = (current * 100) / total if total > 0 else 0
        
        completed_blocks = int(percentage // 10)
        progress_str = "🟦" * completed_blocks + "⬜" * (10 - completed_blocks)
        
        text = (
            f"☁️ **Uploading to Drive:** `{target_name}`\n"
            f"📄 **Current File:** `{current_file_name}`\n"
            f"{progress_str} **{percentage:.1f}%**\n"
            f"💾 `{format_bytes(current)} / {format_bytes(total)}`\n"
            f"🚀 `{format_bytes(speed)}/s` | ⏳ `{int(eta)}s`\n"
        )
        try:
            await status_msg.edit(text)
        except Exception:
            pass # Ignore Telegram "Message is not modified" exceptions

    def drive_progress_sync(current, total, current_file_name):
        """Called by the background thread to schedule an update on the main loop."""
        # Reset speed tracker if the script moves to the next file in a folder
        if current_file_name != current_file_tracker[0]:
            current_file_tracker[0] = current_file_name
            start_time[0] = time.time()
            
        now = time.time()
        # Throttle updates to every 3 seconds to prevent Telegram flood bans
        if (now - last_update_time[0]) < 3 and current != total:
            return
        last_update_time[0] = now
        
        # Safely push the async edit back to the explicit main loop we captured earlier
        asyncio.run_coroutine_threadsafe(
            drive_progress_async(current, total, current_file_name),
            main_loop 
        )

    # --- Register State for Cancellation ---
    global active_gd_uploads
    cancel_flag = {"cancelled": False}
    active_gd_uploads[status_msg.id] = cancel_flag

    try:
        # Run the blocking Google API upload in a background thread, passing callback AND flag
        await asyncio.to_thread(upload_single_target, filepath, drive_progress_sync, cancel_flag)
        
        await status_msg.edit(f"✅ **Google Drive Upload Complete!**\n☁️ **Uploaded:** `{target_name}`\n📂 **Source:** `{filepath}`")
    except Exception as e:
        if cancel_flag.get("cancelled"):
            await status_msg.edit(f"🛑 **Google Drive Upload Cancelled:**\n`{target_name}`")
            logger.info(f"Google Drive upload cancelled by user: {target_name}")
        else:
            logger.exception(f"Google Drive Upload Error: {e}")
            await status_msg.edit(f"❌ **Google Drive Upload Failed:**\n`{str(e)}`")
    finally:
        # Clean up the state tracker
        if status_msg.id in active_gd_uploads:
            del active_gd_uploads[status_msg.id]
        
               
async def ytdl_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        return

    args = event.message.text.split()[1:]
    tg_mode = False
    
    if args and args[0].lower() == 'tg':
        tg_mode = True
        args.pop(0)

    if not args:
        return await event.reply("❌ **Usage:** `/ytdl [tg] <dir_key> [url]`\nOr reply to a `manifest.txt` file.")

    dir_key = f"/{args[0].lower()}"
    if dir_key not in DIRECTORIES:
        return await event.reply(f"❌ Invalid directory. Allowed: `{', '.join(DIRECTORIES.keys())}`")

    dest_dir = DIRECTORIES[dir_key]
    url = args[1] if len(args) > 1 else None
    
    videos_to_process = []
    
    # 1. Manifest Mode
    if not url and event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg.document and reply_msg.file.name.endswith('.txt'):
            status_msg = await event.reply("📄 **Parsing manifest file...**")
            temp_manifest = await reply_msg.download_media(file=os.path.join(dest_dir, f"manifest_{int(time.time())}.txt"))
            try:
                videos_to_process = ytdl.parse_manifest(temp_manifest)
            except Exception as e:
                return await status_msg.edit(f"❌ **Manifest parse error:** `{e}`")
            finally:
                if os.path.exists(temp_manifest): os.remove(temp_manifest)
                
            if not videos_to_process:
                return await status_msg.edit("❌ **No valid entries found in manifest.**")
        else:
            return await event.reply("❌ **Reply must be a .txt manifest file.**")
            
    # 2. Single Link Mode
    elif url:
        videos_to_process.append((url, None, None, None))
        status_msg = await event.reply("⏳ **Initializing YouTube-DL task...**")
    else:
        return await event.reply("❌ **Provide a URL or reply to a manifest file.**")

    channel_id = int(os.getenv("YTDL_CHANNEL_ID", "0"))
    main_loop = asyncio.get_running_loop()

    # Process the queue
    for i, (v_url, v_title, v_start, v_end) in enumerate(videos_to_process):
        success = False
        final_path = None
        safe_title = None
        
        # --- Download Progress Callback ---
        last_dl_update = [0]
        
        async def dl_progress_async(downloaded, total, speed, eta):
            now = time.time()
            if (now - last_dl_update[0]) < 3 and downloaded != total:
                return
            last_dl_update[0] = now
            
            # M3U8 streams sometimes hide total size until finished
            pct = (downloaded * 100) / total if total > 0 else 0
            blocks = int(pct // 10)
            prog_str = "🟦" * blocks + "⬜" * (10 - blocks)
            total_str = format_bytes(total) if total > 0 else "Unknown"
            
            try:
                await status_msg.edit(
                    f"📥 **Downloading ({i+1}/{len(videos_to_process)}):**\n`{v_title or v_url}`\n"
                    f"{prog_str} **{pct:.1f}%**\n"
                    f"💾 `{format_bytes(downloaded)} / {total_str}`\n"
                    f"⬇️ `{format_bytes(speed or 0)}/s` | ⏳ `{int(eta or 0)}s`"
                )
            except Exception:
                pass

        def dl_progress_sync(downloaded, total, speed, eta):
            asyncio.run_coroutine_threadsafe(
                dl_progress_async(downloaded, total, speed, eta),
                main_loop
            )

        # 3-Attempt Retry Logic
        for attempt in range(3):
            try:
                await status_msg.edit(f"📥 **Initializing Download ({i+1}/{len(videos_to_process)}):**\n`{v_title or v_url}`\n*(Attempt {attempt+1}/3)*")
                final_path, safe_title = await asyncio.to_thread(
                    ytdl.download_and_process_sync, v_url, v_title, v_start, v_end, dest_dir, dl_progress_sync
                )
                success = True
                break
            except Exception as e:
                logger.error(f"ytdl attempt {attempt+1} failed for {v_url}: {e}")
                await asyncio.sleep(3)
                
        if not success or not final_path:
            await event.respond(f"❌ **Failed to download after 3 attempts:**\n`{v_title or v_url}`")
            continue
            
        file_size_str = format_bytes(os.path.getsize(final_path))
        
        # Handle Telegram Upload Mode
        if tg_mode:
            start_time = [time.time()]
            last_up_time = [0]
            
            async def upload_progress(current, total):
                now = time.time()
                if (now - last_up_time[0]) < 3 and current != total:
                    return
                last_up_time[0] = now
                elapsed = now - start_time[0]
                speed = current / elapsed if elapsed > 0 else 0
                eta = (total - current) / speed if speed > 0 else 0
                
                pct = (current * 100) / total if total > 0 else 0
                blocks = int(pct // 10)
                prog_str = "🟦" * blocks + "⬜" * (10 - blocks)
                
                try:
                    await status_msg.edit(
                        f"☁️ **Uploading to Channel:**\n`{safe_title}`\n"
                        f"{prog_str} **{pct:.1f}%**\n"
                        f"🚀 `{format_bytes(speed)}/s` | ⏳ `{int(eta)}s`"
                    )
                except Exception:
                    pass

            try:
                w, h, d = await asyncio.to_thread(ytdl.get_video_metadata, final_path)
                thumb_path = await asyncio.to_thread(ytdl.generate_thumbnail, final_path)
                
                logger.info(f"YTDL Uploading to Channel: {final_path}")
                await bot.send_file(
                    channel_id,
                    file=final_path,
                    caption=f"**{safe_title}**\n📁 `{file_size_str}`",
                    supports_streaming=True,
                    thumb=thumb_path,
                    attributes=[DocumentAttributeVideo(duration=d, w=w, h=h)],
                    progress_callback=upload_progress
                )
                
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
                    
                await status_msg.edit(f"✅ **YTDL Task Complete & Uploaded!**\n💾 `{file_size_str}`\n📂 `{final_path}`")
                logger.info(f"YTDL Upload Success: {final_path}")
            except Exception as e:
                logger.exception(f"Telegram Upload Error: {e}")
                await status_msg.edit(f"⚠️ **Downloaded, but TG Upload Failed:**\n`{e}`\n📂 `{final_path}`")
        else:
            await status_msg.edit(f"✅ **YTDL Download Complete!**\n💾 `{file_size_str}`\n📂 `{final_path}`")
            
            
async def search_handler(event):
    """
    Searches a specified channel for keywords and returns an editable JSON dictionary.
    Usage: /search <channel_id> (<search keywords>) [limit]
    """
    if event.sender_id not in ALLOWED_USERS:
        return
        
    entity_str = event.pattern_match.group(1)
    keywords = event.pattern_match.group(2).strip()
    limit_str = event.pattern_match.group(3)
    
    # Use provided limit, otherwise default to 100 to protect memory
    search_limit = int(limit_str) if limit_str else 1000
    status_msg = await event.reply(f"🔍 **Userbot:** Searching for `{keywords}` (Limit: `{search_limit}`)...")
    
    try:
        # Resolve entity (handle private channel IDs safely)
        if entity_str.lstrip('-').isdigit():
            entity = int(entity_str)
            # Ensure proper Telegram API format for supergroups/channels
            if not str(entity).startswith('-100') and not str(entity).startswith('@'):
                entity = int(f"-100{entity}")
        else:
            entity = entity_str

        results = {}
        
        # Utilize server-side search for speed. Limit to 100 to prevent massive memory payloads.
        async for msg in userbot.iter_messages(entity, search=keywords, limit=100):
            # Enforce that the message MUST have a file/media attached
            if msg.media and hasattr(msg, 'file') and msg.file:
                
                # Extract clean channel ID for the t.me link
                chat_id_str = str(msg.chat_id).replace('-100', '')
                link = f"https://t.me/c/{chat_id_str}/{msg.id}"
                
                # Construct a sensible starting filename
                raw_name = None

                # Try the message caption (msg.text) first
                if msg.text and msg.text.strip():
                    # Grab just the first line of the caption to avoid massive descriptions
                    caption_first_line = re.split(r'\s{2,}|\n', msg.text)[0].strip()
                    # Strip illegal characters for Linux/Windows file systems
                    sanitized_caption = re.sub(r'[\\/*?:"<>|]', "", caption_first_line)
                    if sanitized_caption:
                        raw_name = sanitized_caption

                # Use the internal file name if caption was empty/invalid
                if not raw_name:
                    raw_name = getattr(msg.file, 'name', None)

                # Process the name to fix [Errno 36] Long Filename issues
                if raw_name:
                    # Strip leading/trailing underscores, hyphens, tildes, and spaces
                    clean_name = raw_name.strip(" _-~")
                    
                    # Verify and append extension safely
                    _, ext = os.path.splitext(clean_name)
                    
                    # Regex checks if 'ext' looks like a real media extension (e.g. .mkv, .mp4) 
                    # This prevents fake extensions like ".0" from "DDP 2.0" tricking the system
                    is_valid_ext = bool(ext and re.match(r'^\.[a-zA-Z0-9]{2,5}$', ext))
                    # Prevent codecs from registering as file extensions
                    is_codec_fake_ext = ext.lower() in ['.264', '.265']
                    
                    if not is_valid_ext or is_codec_fake_ext:
                        # Grab the file's native extension, or use your custom fallback
                        file_ext = getattr(msg.file, 'ext', None)
                        if not file_ext:
                            file_ext = '.mkv' if "0p" in clean_name.lower() else '.mka'
                        clean_name += file_ext
                        
                    filename = clean_name
                else:
                    filename = f"Media_{msg.id}{getattr(msg.file, 'ext', '.mkv')}"
                    
                # Prevent dictionary key overwrites if files share the exact same name
                original_filename = filename
                dup_counter = 1
                while filename in results:
                    name_part, ext = os.path.splitext(original_filename)
                    filename = f"{name_part}_{dup_counter}{ext}"
                    dup_counter += 1
                
                filename = sanitize_filename(filename)
                results[filename] = link
                
        if not results:
            return await status_msg.edit(f"⚠️ **No media files found matching:** `{keywords}`")
            
        # Format output as an editable JSON block
        if len(results) > search_limit:
            results = dict(list(results.items())[:search_limit])
        json_output = json.dumps(results, indent=4, ensure_ascii=False)
        
        msg_text = (
            f"✅ **Search Complete!** Found `{len(results)}` files.\n"
            f"Edit the names below if needed. **Reply** to this message with `/lmv <Folder Name>` or `/ltv <Folder Name>` to batch download.\n\n"
            f"```json\n{json_output}\n```"
        )
        
        # Failsafe for standard Telegram character limits
        if len(msg_text) > 4096:
            temp_file = f"search_results_{int(time.time())}.json"
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(json_output)
            await event.reply(
                file=temp_file, 
                message="✅ **Search Complete!** Results were too large for text. Edit this file, paste the contents back here, and reply to it with your `/lmv` command."
            )
            os.remove(temp_file)
            await status_msg.delete()
        else:
            await status_msg.edit(msg_text)
            
    except Exception as e:
        logger.exception("Search handler failed")
        await status_msg.edit(f"❌ **Search Error:** `{str(e)}`")
        
        
        