import os
import math
import asyncio
import logging
from telethon import utils
from telethon.tl.functions.upload import GetFileRequest

logger = logging.getLogger(__name__)

def write_chunk(path, offset, data):
    """Thread-safe synchronous write at a specific byte offset."""
    with open(path, 'r+b') as f:
        f.seek(offset)
        f.write(data)

async def parallel_download(client, message, dest_path, progress_callback=None, workers=4):
    """
    Multiplexed parallel chunking downloader for Telethon.
    Saturates network bandwidth by avoiding the single-request ping-pong bottleneck.
    """
    media = message.media
    if not media or not hasattr(message, 'file') or not message.file:
        return None

    file_size = message.file.size
    # Telegram max chunk size is 1MB. We use 512KB for safety and stability.
    chunk_size = 512 * 1024 
    
    # If the file is tiny (< 10MB), overhead isn't worth it. Use standard download.
    if file_size < 10 * 1024 * 1024:
        return await client.download_media(message, file=dest_path, progress_callback=progress_callback)

    location = utils.get_input_location(media)
    if not location:
        return await client.download_media(message, file=dest_path, progress_callback=progress_callback)

    # Pre-allocate file on disk to prevent fragmentation on the Pi's storage
    with open(dest_path, 'wb') as f:
        f.truncate(file_size)

    # Calculate chunks and build the queue
    chunks = math.ceil(file_size / chunk_size)
    queue = asyncio.Queue()
    for i in range(chunks):
        queue.put_nowait(i * chunk_size)

    downloaded_bytes = 0
    lock = asyncio.Lock()
    
    async def worker():
        nonlocal downloaded_bytes
        while True:
            try:
                offset = queue.get_nowait()
            except asyncio.QueueEmpty:
                break # Queue exhausted, worker dies gracefully
                
            try:
                # 1. Fetch chunk from Telegram
                result = await client(GetFileRequest(
                    location=location,
                    offset=offset,
                    limit=chunk_size
                ))
                
                # 2. Write chunk to disk asynchronously to protect main loop
                await asyncio.to_thread(write_chunk, dest_path, offset, result.bytes)
                
                # 3. Safely increment progress
                async with lock:
                    downloaded_bytes += len(result.bytes)
                    if progress_callback:
                        try:
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(downloaded_bytes, file_size)
                            else:
                                progress_callback(downloaded_bytes, file_size)
                        except Exception:
                            pass # Failsafe against UI flood errors
                            
            except asyncio.CancelledError:
                raise # Honor global /cancel commands
            except Exception as e:
                logger.error(f"Chunk error at offset {offset}: {e}")
                await asyncio.sleep(1) # Prevent infinite CPU spinning on hard fail
                queue.put_nowait(offset) # Put the chunk back in the queue to try again

    # Spawn our parallel workers
    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # If the user issues /cancel, intercept it, kill workers, and nuke the half-downloaded file
        for task in tasks:
            task.cancel()
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise

    return dest_path


