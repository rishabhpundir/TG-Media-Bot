import os
from dotenv import load_dotenv

load_dotenv(override=True)

# --- Telegram Configuration ---
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS_ = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [int(x.strip()) for x in ALLOWED_USERS_.split(",") if x.strip()]

# --- Aria2c RPC Configuration ---
ARIA2_RPC_URL = os.getenv("ARIA2_RPC_URL", "http://localhost:6800/jsonrpc")
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET", "")

# Directory Paths
DIRECTORIES = {
    '/mv': '/mnt/blue/movies',
    '/tv': '/mnt/blue/tv',
    '/lmv': '/mnt/blue/movies',
    '/ltv': '/mnt/blue/tv',
    '/mv2': '/mnt/media/movies',
    '/tv2': '/mnt/media/tv',
    '/lmv2': '/mnt/media/movies',
    '/ltv2': '/mnt/media/tv',
    '/docu': '/mnt/blue/docu'
}

# --- Google Drive Target Folders ---
# Local dir-key -> Drive folder. mv2/tv2 intentionally share the same Drive
# counterparts as mv/tv (same media type, different local mount).
DRIVE_FOLDERS = {
    'mv':  os.getenv("MOVIES_DRIVE_FOLDER_ID"),
    'tv':  os.getenv("TV_DRIVE_FOLDER_ID"),
    'mv2': os.getenv("MOVIES_DRIVE_FOLDER_ID"),
    'tv2': os.getenv("TV_DRIVE_FOLDER_ID"),
}

DRIVE_FOLDER_LABELS = {
    'mv':  os.getenv("MOVIES_DRIVE_FOLDER_NAME", "1#movies"),
    'tv':  os.getenv("TV_DRIVE_FOLDER_NAME", "2#tv"),
    'mv2': os.getenv("MOVIES_DRIVE_FOLDER_NAME", "1#movies"),
    'tv2': os.getenv("TV_DRIVE_FOLDER_NAME", "2#tv"),
}

# Legacy single-folder fallback (used by /docu and any unmapped path)
TARGET_DRIVE_FOLDER_ID = os.getenv("TARGET_DRIVE_FOLDER_ID")
DEFAULT_DRIVE_LABEL = os.getenv("TARGET_DRIVE_FOLDER_NAME", "default")


def drive_target_for_key(key):
    """'mv' | '/tv' | 'LMV' -> (folder_id, label, local_base_dir)."""
    k = (key or "").strip().lower().lstrip('/')
    if k.startswith('l') and f"/{k[1:]}" in DIRECTORIES:   # /lmv, /ltv aliases
        k = k[1:]
    base = DIRECTORIES.get(f"/{k}")
    fid = DRIVE_FOLDERS.get(k)
    if fid:
        return fid, DRIVE_FOLDER_LABELS.get(k, k), base
    return TARGET_DRIVE_FOLDER_ID, DEFAULT_DRIVE_LABEL, base


def drive_target_for_path(path):
    """Absolute local path -> (folder_id, label, local_base_dir), longest-prefix match."""
    real = os.path.realpath(path)
    best_key, best_base, best_len = None, None, -1
    for key, base in DIRECTORIES.items():
        k = key.lstrip('/')
        if not DRIVE_FOLDERS.get(k):
            continue
        rbase = os.path.realpath(base)
        if (real == rbase or real.startswith(rbase + os.sep)) and len(rbase) > best_len:
            best_key, best_base, best_len = k, base, len(rbase)
    if best_key:
        return DRIVE_FOLDERS[best_key], DRIVE_FOLDER_LABELS.get(best_key, best_key), best_base
    return TARGET_DRIVE_FOLDER_ID, DEFAULT_DRIVE_LABEL, None


# Constraints
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS"))
MAX_FILE_SIZE_GB = 32
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024


