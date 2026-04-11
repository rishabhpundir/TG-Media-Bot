import asyncio

# --- STATE MANAGEMENT ---
queue = asyncio.Queue()
active_downloads = {} 
pending_deletions = {}
pending_aria_actions = {}
current_concurrent_count = 0


