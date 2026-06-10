import os
import re
import shlex
import base64
import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit, unquote, quote

logger = logging.getLogger(__name__)

RCLONE_BIN = os.getenv("RCLONE_BIN", "rclone")
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "gdrive")  # your `rclone config` remote name
RCLONE_EXTRA_ARGS = os.getenv("RCLONE_EXTRA_ARGS", "")  # e.g. "--user-agent curl --disable-http2"
TARGET_DRIVE_FOLDER_ID = os.getenv("TARGET_DRIVE_FOLDER_ID")

# rclone --stats-one-line emits, e.g.:
#   "... INFO  : Transferred: 1.234 GiB / 5.678 GiB, 22%, 45.6 MiB/s, ETA 1m30s"
#   "... INFO  : Transferred: 50.0 MiB / 50.0 MiB, -, 10.0 MiB/s, ETA -"   (size unknown)
_XFER_RE = re.compile(r'Transferred:\s*(?P<body>\S.*?)\s*$')
_PCT_RE = re.compile(r',\s*(?P<pct>\d+)\s*%')
_EXT_RE = re.compile(r'\.[A-Za-z0-9]{1,5}$')


def derive_filename(url):
    """Pull a usable filename from the URL path (URL-decoded).

    Returns a name only if it carries a plausible extension; otherwise None,
    which signals the caller to let rclone read the name from response headers.
    """
    name = unquote(os.path.basename(urlsplit(url).path)).strip()
    if name and _EXT_RE.search(name):
        return name
    return None


def _inject_basic_auth(url, username, password):
    """Embed HTTP Basic credentials in the URL userinfo (percent-encoded).

    Scopes the auth to the source GET request only. rclone's global --header
    flag would instead attach the header to *every* transaction — including the
    Google Drive API calls — which overwrites rclone's OAuth Bearer token and
    yields a 403 'unregistered callers' error.
    """
    parts = urlsplit(url)
    host = parts.netloc.rsplit('@', 1)[-1]          # drop any existing userinfo
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _redact_url(arg):
    """Mask credentials in a URL userinfo so they don't end up in the logs."""
    if "://" not in arg:
        return arg
    parts = urlsplit(arg)
    if "@" not in parts.netloc:
        return arg
    host = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


async def stream_url_to_drive(url, status_callback=None, cancel_flag=None, filename=None, username=None, password=None):
    """Stream a direct-download URL straight into Google Drive via `rclone copyurl`.

    No bytes touch local disk. status_callback(pct, body) is awaited per stats
    line: pct is an int 0-100, or None when the source omits Content-Length.
    cancel_flag is a dict like {"cancelled": False}; set True to abort.
    """
    if not TARGET_DRIVE_FOLDER_ID:
        raise Exception("TARGET_DRIVE_FOLDER_ID is not set in .env")

    name = filename or derive_filename(url)

    # Basic auth for the SOURCE only: embed it in the URL userinfo so it rides
    # on the source GET request and never leaks onto the Drive API calls.
    fetch_url = url
    if username and password:
        fetch_url = _inject_basic_auth(url, username, password)

    cmd = [RCLONE_BIN, "copyurl", fetch_url]
    if name:
        cmd.append(f"{RCLONE_REMOTE}:{name}")
    else:
        cmd.append(f"{RCLONE_REMOTE}:")
        cmd += ["--auto-filename", "--header-filename"]

    cmd += [
        "--drive-root-folder-id", TARGET_DRIVE_FOLDER_ID,
        "--stats", "2s",
        "--stats-one-line",
        "-v",
    ]
    if RCLONE_EXTRA_ARGS.strip():
        cmd += shlex.split(RCLONE_EXTRA_ARGS)

    # Redact any embedded credentials before logging.
    safe_cmd = " ".join(_redact_url(a) for a in cmd)
    logger.info("rclone stream start (%s): %s", name or "auto-name", safe_cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # merge so we read one stream
    )

    buf = ""
    last_line = ""
    try:
        while True:
            if cancel_flag and cancel_flag.get("cancelled"):
                proc.kill()
                await proc.wait()
                raise Exception("Upload Cancelled")

            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # poll the cancel flag even while rclone is quiet

            if not chunk:
                break  # EOF -> rclone finished

            buf += chunk.decode(errors="replace")
            parts = re.split(r'[\r\n]', buf)   # \n for logs, \r for progress redraws
            buf = parts.pop()                  # keep trailing incomplete fragment
            for line in parts:
                line = line.strip()
                if not line:
                    continue
                last_line = line
                if status_callback and "Transferred:" in line:
                    xm = _XFER_RE.search(line)
                    body = xm.group("body") if xm else line
                    pm = _PCT_RE.search(line)
                    pct = int(pm.group("pct")) if pm else None
                    await status_callback(pct, body)

        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    cancelled = bool(cancel_flag and cancel_flag.get("cancelled"))
    if proc.returncode != 0 and not cancelled:
        raise Exception(f"rclone exited with code {proc.returncode}: {last_line}")
    
    
    