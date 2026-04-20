import os
import time
import asyncio

from core.utils import format_bytes
from config import MAX_CONCURRENT_DOWNLOADS
from state import queue, active_downloads, current_concurrent_count


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
      

