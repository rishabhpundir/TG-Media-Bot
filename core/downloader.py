import os
import json
import time
import asyncio
import logging
from datetime import datetime

from telethon.errors import FileReferenceExpiredError

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


# =================== DOWNLOAD LEDGER ===================
# Tracks completed downloads by Telegram's permanent file_id (NOT filename,
# NOT path) so duplicates are detected even after renames or moves.
# Flat JSON dict, written atomically via tmp+rename to survive crashes.

# Lives at project root, alongside main.py.
DOWNLOAD_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    'download_ledger.json'
)

_ledger_lock = asyncio.Lock()
_ledger_cache = None  # Lazy-loaded on first access; kept in sync with disk.


def _get_file_uid(media_msg):
    """
    Stable Telegram-side identifier for the file. Works for documents
    (videos, audio, generic files) and photos. Returns None for media
    types we don't track (contact cards, polls, etc.).
    """
    media = getattr(media_msg, 'media', None)
    if media is None:
        return None
    doc = getattr(media, 'document', None)
    if doc is not None:
        return f"doc:{doc.id}"
    photo = getattr(media, 'photo', None)
    if photo is not None:
        return f"photo:{photo.id}"
    return None


def _ledger_load_sync():
    """Sync read — must be called via asyncio.to_thread."""
    if not os.path.exists(DOWNLOAD_LEDGER_PATH):
        return {}
    try:
        with open(DOWNLOAD_LEDGER_PATH, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Ledger read failed ({e}); starting empty for this session.")
        return {}


def _ledger_save_sync(data):
    """Sync write with atomic tmp+rename — must be called via asyncio.to_thread."""
    tmp = DOWNLOAD_LEDGER_PATH + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DOWNLOAD_LEDGER_PATH)


async def _ledger_get(file_uid):
    """Return ledger entry for a file UID, or None."""
    global _ledger_cache
    async with _ledger_lock:
        if _ledger_cache is None:
            _ledger_cache = await asyncio.to_thread(_ledger_load_sync)
        return _ledger_cache.get(file_uid)


async def _ledger_set(file_uid, entry):
    """Record a completed download and flush to disk."""
    global _ledger_cache
    async with _ledger_lock:
        if _ledger_cache is None:
            _ledger_cache = await asyncio.to_thread(_ledger_load_sync)
        _ledger_cache[file_uid] = entry
        # Snapshot so the (slow) disk write can't race with future mutations.
        snapshot = dict(_ledger_cache)
        await asyncio.to_thread(_ledger_save_sync, snapshot)


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
            sender = getattr(state, 'sender', None) or getattr(state, '_sender', None)
            if sender is None:
                continue
            try:
                # Mark disconnected first so any in-flight reconnect logic
                # inside Telethon sees a terminal state and stops spawning
                # new send/recv loop tasks.
                sender._user_connected = False
                await sender.disconnect()
            except Exception as e:
                logger.debug(f"Sender disconnect during reset (ignored): {e}")
            # Let the cancelled send_loop/recv_loop tasks actually finish
            # tearing down before we return — eliminates the
            # "Task was destroyed but it is pending" warnings.
            await asyncio.sleep(0.2)


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

    # ===== LEDGER DEDUP CHECK =====
    # Keyed on Telegram's permanent file_id, so we catch re-downloads even
    # if the user renamed/moved the file. If the ledger says we have it but
    # the recorded path no longer exists, treat the entry as stale and
    # proceed to re-download.
    file_uid = _get_file_uid(media_msg)
    if file_uid:
        existing = await _ledger_get(file_uid)
        if existing and os.path.exists(existing.get('path', '')):
            await status_msg.edit(
                f"⏭️ **Already downloaded — skipping**\n"
                f"`{clean_name}`\n"
                f"📂 Existing: `{existing['path']}`\n"
                f"📅 On: `{existing.get('downloaded_at', 'unknown')}`"
            )
            return

    if os.path.exists(final_path):
        await status_msg.edit(f"❌ **Error:** File already exists.\n`{clean_name}`")
        return

    client = media_msg._client

    # ===== PROACTIVE FILE_REFERENCE REFRESH =====
    # file_reference tokens expire after ~30-60 min. Queued items may have
    # waited hours before reaching the worker, so re-fetch the source
    # message right before starting to get a fresh reference + fresh TTL.
    try:
        refreshed = await client.get_messages(media_msg.peer_id, ids=media_msg.id)
        if refreshed is None or refreshed.media is None:
            await status_msg.edit(
                f"❌ **Error:** Source message no longer available.\n`{clean_name}`"
            )
            return
        media_msg = refreshed
    except Exception as e:
        # Refresh failure is non-fatal — try with the stale reference.
        # If it has expired, the in-loop handler below will catch it.
        logger.warning(f"Pre-download refresh failed for {clean_name}: {e}")

    start_time = time.time()
    last_update = [0]
    file_size = media_msg.file.size
    downloaded = 0

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

                except FileReferenceExpiredError:
                    # Reference died mid-download (only happens for files
                    # that take longer than the TTL — rare at single concurrency
                    # but possible on very large files or slow links).
                    logger.warning(
                        f"File reference expired mid-download for {clean_name} "
                        f"at {downloaded}/{file_size} bytes — refreshing."
                    )
                    await status_msg.edit(
                        f"🔄 **Refreshing reference**\n"
                        f"`{clean_name}`\n"
                        f"💾 `{format_bytes(downloaded)} / {format_bytes(file_size)}`"
                    )
                    try:
                        refreshed = await client.get_messages(
                            media_msg.peer_id, ids=media_msg.id
                        )
                        if refreshed is None or refreshed.media is None:
                            raise ConnectionError("Source message gone during refresh")
                        media_msg = refreshed
                    except FileReferenceExpiredError:
                        # Should be impossible — re-raise to outer handler.
                        raise
                    except Exception as e:
                        raise ConnectionError(f"Reference refresh failed: {e}")
                    # No backoff, no sender reset — just loop again with the
                    # fresh reference. Counts as one retry attempt; that's fine.
                    continue

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
                        f"⚠️ **Stalled — retrying ({attempt + 1}/{MAX_RETRIES + 1})**\n"
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

        # Record in ledger so future runs skip this file by Telegram file_id,
        # regardless of where the user later moves or renames it on disk.
        if file_uid:
            await _ledger_set(file_uid, {
                'filename': clean_name,
                'path': final_path,
                'size': os.path.getsize(final_path),
                'downloaded_at': datetime.now().isoformat(timespec='seconds'),
                'channel_id': getattr(media_msg, 'chat_id', None),
                'message_id': media_msg.id,
            })

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


