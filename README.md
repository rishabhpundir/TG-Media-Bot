# Telegram Advanced Download & File Manager Bot

A feature-rich Telegram bot designed to manage media downloads, t0rrents, and file system operations remotely (via termux on android). Built to run on a Raspberry Pi, it acts as a complete control center for grabbing media from Telegram or the web and organizing it locally, so they can be streamed directly using plex media server or jellyfin.

## ✨ Features

* **Dual-Client Architecture**: Utilizes both a standard Telegram Bot and a Userbot to fetch media, allowing it to bypass restrictions and grab files from private or restricted channels.
* **Aria2c Integration**: 
    * Send Magnet links, direct HTTP/FTP URLs, or reply to `.torrent` files to initiate external downloads.
    * Live, dynamically updating progress tracking directly in the Telegram chat (Speed, ETA, Seeders, Progress bar).
    * Full task control: Pause, resume, remove, and safely delete active or stopped Aria2 downloads.
* **Telegram Media Downloader**: 
    * Directly download media files sent in the chat into categorized local folders (e.g., Movies, TV).
    * Built-in queue system for concurrent download management to prevent overloading the system or hitting rate limits.
* **Robust Archive Management (`/unzip`)**:
    * Extract compressed files (`.zip`, `.rar`, `.tar`, `.7z`, etc.) directly from the chat interface.
    * Keyword-based searching to find and extract specific archives automatically.
    * Optional automatic cleanup (deletion) of the original archive after a successful extraction.
* **Comprehensive File Manager (`/fm`)**:
    * Browse internal server directories and explore folder contents natively within Telegram.
    * Move, delete, and individually rename files or folders.
    * **Bulk Renaming**: Smart alphabetical renaming of TV show episodes or files in a directory using dynamic `{NUM}` sequential patterns.
* **Chat & State Management**: Keep the UI clean with instant non-pinned message sweeping (`/cls`) and explore functionalities via dynamic, module-specific help commands (`/cmd`).

## 🛠️ Tech Stack

* **Language**: Python 3.10+
* **Telegram Framework**: [Telethon](https://docs.telethon.dev/) (Async MTProto API handling both Bot and Userbot sessions)
* **Download Engine**: Aria2 (via JSON-RPC interface)
* **Networking**: `aiohttp` (for async RPC requests to the Aria2 server)
* **System Tools**: Native `subprocess`, `shutil`, and `os` modules combined with Linux binaries (`unrar`, `p7zip-full`) for advanced file and archive manipulation.


