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

# Global lock to prevent Telethon SQLite/DC-Auth deadlocks
dc_auth_lock = asyncio.Lock()


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
            # iter_download() is lazy — constructing it does NOT hit the network
            # or the SQLite session. Only iteration triggers DC auth.
            chunk_generator = client.iter_download(media_msg.media, request_size=1048576)

            # First chunk: 45s budget covers DC reconnect + auth handshake worst-case.
            FIRST_CHUNK_TIMEOUT = 45
            CHUNK_TIMEOUT = 30

            # ===== TURNSTILE LOCK =====
            async with dc_auth_lock:
                try:
                    first_chunk = await asyncio.wait_for(
                        anext(chunk_generator), timeout=FIRST_CHUNK_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    raise ConnectionError(
                        f"First-chunk timeout after {FIRST_CHUNK_TIMEOUT}s — "
                        "borrowed sender for this DC is likely dead (TCP black hole). "
                        "Restart the bot to force a fresh sender pool."
                    )
                except StopAsyncIteration:
                    first_chunk = None
            # ===== LOCK RELEASED — sender is now cached for this DC =====

            if first_chunk is not None:
                await asyncio.to_thread(sync_write, f, first_chunk)
                downloaded += len(first_chunk)
                await progress_bar(downloaded, file_size, status_msg, start_time, last_update, clean_name)

            # Per-chunk watchdog: 30s of zero progress = stalled connection, bail out cleanly.
            # Using explicit anext() instead of `async for` so we can wrap each pull in wait_for.
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        anext(chunk_generator), timeout=CHUNK_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    raise ConnectionError(
                        f"Chunk timeout after {CHUNK_TIMEOUT}s at {downloaded}/{file_size} bytes — "
                        "stream stalled. The other 2 workers may still be progressing."
                    )
                except StopAsyncIteration:
                    break

                await asyncio.to_thread(sync_write, f, chunk)
                downloaded += len(chunk)
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
      

