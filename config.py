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
    '/ltv2': '/mnt/media/tv'
}

# Constraints
MAX_CONCURRENT_DOWNLOADS = 2
MAX_FILE_SIZE_GB = 32
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024


