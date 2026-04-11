import asyncio
import aiohttp
import logging

logger = logging.getLogger(__name__)

from utils import format_bytes
from config import ARIA2_RPC_URL, ARIA2_RPC_SECRET


async def aria2_request(method, params=None):
    """Sends an async JSON-RPC request to the Aria2c server."""
    # Create a safe copy to prevent mutating list references
    safe_params = list(params) if params else []
        
    # If a secret token is set, prepend it to the parameters
    if ARIA2_RPC_SECRET:
        safe_params.insert(0, f"token:{ARIA2_RPC_SECRET}")

    payload = {
        "jsonrpc": "2.0",
        "id": "tg_bot",
        "method": f"aria2.{method}",
        "params": safe_params
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(ARIA2_RPC_URL, json=payload) as resp:
                result = await resp.json()
                if "error" in result:
                    raise Exception(result["error"].get("message", "Unknown Aria2 Error"))
                return result.get("result")
    except Exception as e:
        raise Exception(f"Aria2 Connection Error: {str(e)}")


async def aria2_progress_tracker(gid, status_msg, filename):
    """Tracks the live progress of an Aria2 download and updates the Telegram message."""
    error_count = 0
    
    while True:
        try:
            status = await aria2_request("tellStatus", [gid])
            error_count = 0 # Reset error threshold on successful ping
            
            state = status.get("status")
            total_length = int(status.get("totalLength", 0))
            completed_length = int(status.get("completedLength", 0))
            download_speed = int(status.get("downloadSpeed", 0))
            upload_speed = int(status.get("uploadSpeed", 0))
            connections = status.get("connections", "0")
            seeders = status.get("numSeeders", "0")
            dir_path = status.get("dir", "Unknown")

            if state == "complete":
                await status_msg.edit(
                    f"✅ **Aria2 Download Complete!**\n"
                    f"🆔 **GID:** `{gid}`\n"
                    f"💾 **Size:** `{format_bytes(total_length)}`\n"
                    f"📂 **Path:** `{dir_path}`\n"
                    f"🏷️ **Name:** `{filename}`"
                )
                break
            elif state in ["error", "removed"]:
                error_msg = status.get("errorMessage", "Unknown error or cancelled.")
                await status_msg.edit(f"❌ **Aria2 Download Failed/Cancelled:**\n🆔 **GID:** `{gid}`\n`{error_msg}`")
                break
            elif state == "paused":
                await status_msg.edit(
                    f"⏸️ **Aria2 Task Paused**\n"
                    f"🆔 **GID:** `{gid}`\n"
                    f"🏷️ **Name:** `{filename}`\n\n"
                    f"*(Reply with `/aria start` to resume, or check `/aria list`)*"
                )
                break # Safely exit the loop. The user can spawn a new tracker later if they want.

            # Calculate metrics
            percentage = (completed_length * 100 / total_length) if total_length > 0 else 0
            eta = (total_length - completed_length) / download_speed if download_speed > 0 else 0
            
            completed_blocks = int(percentage // 10)
            progress_str = "🟦" * completed_blocks + "⬜" * (10 - completed_blocks)

            is_torrent = "bittorrent" in status
            
            text = (
                f"🌩️ **Aria2 Downloading:** `{filename}`\n"
                f"🆔 **GID:** `{gid}`\n"
                f"{progress_str} **{percentage:.1f}%**\n"
                f"💾 `{format_bytes(completed_length)} / {format_bytes(total_length)}`\n"
                f"⬇️ `{format_bytes(download_speed)}/s` | ⬆️ `{format_bytes(upload_speed)}/s`\n"
                f"⏳ **ETA:** `{int(eta)}s`\n"
            )

            if is_torrent:
                text += f"🔗 **Peers:** `{connections}` | 🌱 **Seeds:** `{seeders}`\n"
            else:
                text += f"🔌 **Connections:** `{connections}`\n"

            text += f"📂 **Dest:** `{dir_path}`"
            
            await status_msg.edit(text)
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.exception(f"Aria2 Tracker Error: {e}")
            error_count += 1
            if error_count >= 3:
                try:
                    await status_msg.edit(f"⚠️ **Lost connection to Aria2c Tracker.**\n🆔 **GID:** `{gid}`\n*(Check `/aria list` to see if it's still running)*")
                except: pass
                break
            await asyncio.sleep(5)
            
            
            