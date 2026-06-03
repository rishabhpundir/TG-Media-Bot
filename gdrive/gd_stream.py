import os
import re
import asyncio
import logging

logger = logging.getLogger(__name__)

RCLONE_BIN = os.getenv("RCLONE_BIN", "rclone")
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "gdrive")  # name of your `rclone config` remote
TARGET_DRIVE_FOLDER_ID = os.getenv("TARGET_DRIVE_FOLDER_ID")

# Matches rclone --stats-one-line, e.g.:
# Transferred:   1.234 GiB / 5.678 GiB, 22%, 45.6 MiB/s, ETA 1m30s
_STATS_RE = re.compile(
    r'Transferred:\s+[\d.]+\s*\w+\s*/\s*[\d.]+\s*\w+,\s*(?P<pct>\d+)%'
)


async def stream_url_to_drive(url, status_callback=None, cancel_flag=None, filename=None):
    """Stream a direct-download URL straight into Google Drive via `rclone copyurl`.

    rclone fetches the URL and pipes the HTTP body directly into a Drive
    resumable-upload session. Nothing is written to local disk; memory stays
    at roughly one upload chunk regardless of file size.

    status_callback(pct:int, line:str) is awaited on each stats update.
    cancel_flag is a dict like {"cancelled": False}; set True to abort.
    """
    if not TARGET_DRIVE_FOLDER_ID:
        raise Exception("TARGET_DRIVE_FOLDER_ID is not set in .env")

    dest = f"{RCLONE_REMOTE}:{filename}" if filename else f"{RCLONE_REMOTE}:"
    cmd = [
        RCLONE_BIN, "copyurl", url, dest,
        "--drive-root-folder-id", TARGET_DRIVE_FOLDER_ID,
        "--stats", "2s",
        "--stats-one-line",
        "-v",
    ]
    if not filename:
        # derive name from Content-Disposition, falling back to the URL tail
        cmd += ["--auto-filename", "--header-filename"]

    logger.info("rclone stream start: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # merge so we read one stream
    )

    last_line = ""
    try:
        while True:
            if cancel_flag and cancel_flag.get("cancelled"):
                proc.kill()
                await proc.wait()
                raise Exception("Upload Cancelled")

            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # lets us poll the cancel flag even while rclone is quiet

            if not raw:
                break  # EOF -> rclone finished

            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            last_line = line

            m = _STATS_RE.search(line)
            if m and status_callback:
                await status_callback(int(m.group("pct")), line)

        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    cancelled = bool(cancel_flag and cancel_flag.get("cancelled"))
    if proc.returncode != 0 and not cancelled:
        raise Exception(f"rclone exited with code {proc.returncode}: {last_line}")
    
    
    