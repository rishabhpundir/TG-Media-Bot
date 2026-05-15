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

# Coordinates force-resets of Telethon's borrowed sender pool when a TCP
# black hole is detected. Cooldown prevents 3 simultaneously-timing-out
# workers from each tearing down the pool.
_sender_reset_lock = asyncio.Lock()
_sender_last_reset = 0.0


async def _reset_borrowed_senders(client):
    """
    Force-disconnect and clear Telethon's cached borrowed senders for the
    given client. Subsequent iter_download calls will re-export auth and
    open fresh TCP connections.
    """
    global _sender_last_reset
    async with _sender_reset_lock:
        # 5s cooldown: if another worker just reset, skip.
        if time.time() - _sender_last_reset < 5:
            return
        _sender_last_reset = time.time()

        borrowed = getattr(client, '_borrowed_senders', None) or {}
        if not borrowed:
            return

        dc_ids = list(borrowed.keys())
        logger.warning(f"Force-resetting dead borrowed senders for DCs: {dc_ids}")

        for dc_id in dc_ids:
            state = borrowed.pop(dc_id, None)
            if state is None:
                continue
            # Telethon's _ExportState stores the actual sender; attribute name
            # varies slightly across versions, so probe both defensively.
            sender = getattr(state, 'sender', None) or getattr(state, '_sender', None)
            if sender is not None:
                try:
                    await sender.disconnect()
                except Exception as e:
                    logger.debug(f"Sender disconnect during reset (ignored): {e}")


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
    file_size = media_msg.file.size
    downloaded = 0
    client = media_msg._client

    def sync_write(fd, data):
        fd.write(data)

    FIRST_CHUNK_TIMEOUT = 45
    CHUNK_TIMEOUT = 30
    MAX_RETRIES = 5

    try:
        # 'ab' so retries resume from the partial bytes already on disk
        # rather than re-downloading from zero.
        with open(temp_path, 'ab') as f:
            for attempt in range(MAX_RETRIES + 1):
                # Resume from current offset. Telegram requires offset to be
                # divisible by 4KB; since chunks come back at 1MB granularity,
                # `downloaded` stays naturally aligned.
                chunk_generator = client.iter_download(
                    media_msg.media,
                    offset=downloaded,
                    request_size=1048576
                )

                try:
                    # Turnstile: serialize first-chunk DC auth (applies on every
                    # retry too, because a fresh sender means fresh auth).
                    async with dc_auth_lock:
                        first_chunk = await asyncio.wait_for(
                            anext(chunk_generator), timeout=FIRST_CHUNK_TIMEOUT
                        )

                    # Process first chunk outside the lock.
                    await asyncio.to_thread(sync_write, f, first_chunk)
                    downloaded += len(first_chunk)
                    await progress_bar(downloaded, file_size, status_msg, start_time, last_update, clean_name)

                    # Stream remaining chunks with per-chunk watchdog.
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                anext(chunk_generator), timeout=CHUNK_TIMEOUT
                            )
                        except StopAsyncIteration:
                            break
                        await asyncio.to_thread(sync_write, f, chunk)
                        downloaded += len(chunk)
                        await progress_bar(downloaded, file_size, status_msg, start_time, last_update, clean_name)

                    # Reached here = full download succeeded.
                    break

                except StopAsyncIteration:
                    # Empty file edge case (first __anext__ already empty).
                    break

                except asyncio.TimeoutError:
                    if attempt >= MAX_RETRIES:
                        raise ConnectionError(
                            f"Persistent stall after {MAX_RETRIES + 1} attempts. "
                            f"Stuck at {downloaded}/{file_size} bytes."
                        )
                    logger.warning(
                        f"Stream stalled at {downloaded}/{file_size} bytes "
                        f"(attempt {attempt + 1}/{MAX_RETRIES + 1}). "
                        f"Resetting senders, retrying."
                    )
                    await status_msg.edit(
                        f"⚠️ **Stalled — retrying ({attempt + 1}/{MAX_RETRIES})**\n"
                        f"`{clean_name}`\n"
                        f"💾 `{format_bytes(downloaded)} / {format_bytes(file_size)}`"
                    )
                    # Tear down the dead borrowed sender so the retry gets a
                    # fresh DC connection + fresh auth.
                    await _reset_borrowed_senders(client)
                    # Linear backoff (2s, 4s, 6s, 8s, 10s) — light enough that
                    # transient blips recover fast, heavy enough that we don't
                    # hammer Telegram during a real outage.
                    await asyncio.sleep(2 * (attempt + 1))

        os.rename(temp_path, final_path)

        file_size_str = format_bytes(os.path.getsize(final_path))
        await status_msg.edit(
            f"✅ **Download Complete!**\n"
            f"💾 **Size:** `{file_size_str}`\n"
            f"📂 **Path:** `{final_path}`"
        )

    except asyncio.CancelledError:
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit("🛑 **Download Cancelled & Cleaned Up.**")
        raise
    except Exception as e:
        logger.error(f"Telegram Download error: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)
        await status_msg.edit(f"❌ **Error downloading:** `{e}`")


