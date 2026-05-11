import os
import time
import asyncio
import logging

from core.utils import format_bytes
from config import MAX_CONCURRENT_DOWNLOADS
from state import queue, active_downloads, current_concurrent_count

logger = logging.getLogger(__name__)

bot = None
userbot = None


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

    # NATIVE HIGH-SPEED CHUNKING
    file_size = media_msg.file.size
    downloaded = 0
    client = media_msg._client

    # Thread-safe write helper
    def sync_write(fd, data):
        fd.write(data)

    try:
        # Open the file once to avoid high disk I/O overhead
        with open(temp_path, 'wb') as f:
            # request_size=1048576 forces Telegram's absolute max 1MB packet size
            async for chunk in client.iter_download(media_msg.media, request_size=1048576):
                
                # Push the 1MB write to a background thread to protect the async loop
                await asyncio.to_thread(sync_write, f, chunk)
                downloaded += len(chunk)
                
                # Natively update UI without spamming task queues 
                # (Your progress_bar handles the 3-second throttle natively)
                await progress_bar(downloaded, file_size, status_msg, start_time, last_update, clean_name)

        os.rename(temp_path, final_path)
        
        file_size_str = format_bytes(os.path.getsize(final_path))
        await status_msg.edit(
            f"✅ **Download Complete!**\n"
            f"💾 **Size:** `{file_size_str}`\n"
            f"📂 **Path:** `{final_path}`"
        )

    except asyncio.CancelledError:
        # Standard task cancellation automatically intercepts here
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit("🛑 **Download Cancelled & Cleaned Up.**")
        raise # Pass bubble up to clean state
    except Exception as e:
        logger.error(f"Telegram Download error: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit(f"❌ **Error downloading:** `{e}`")
      

