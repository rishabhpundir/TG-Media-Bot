# Telegram Advanced Download & File Manager Bot
A feature-rich Telegram bot designed to manage media downloads, torrents, cloud uploads, and file system operations remotely (via termux on android or natively on a Raspberry Pi). It acts as a complete control center for grabbing media from Telegram, the web, or streaming platforms and organizing it locally so they can be streamed directly using Plex Media Server or Jellyfin.


## ✨ Features
* **Dual-Client Architecture**: Utilizes both a standard Telegram Bot and a Userbot to fetch media, allowing it to bypass restrictions and grab files from private or restricted channels.
* **Smart Search & Batch JSON Downloads (NEW)**:
    * Search any channel using your Userbot via keywords and retrieve a JSON list of matching files.
    * Reply to the JSON file with a single command to automatically batch-download all matched files into a custom folder. 
* **YouTube-DL & Stream Harvesting (NEW)**:
    * Download streams or videos directly into designated folders using `yt-dlp`.
    * Supports a dedicated Telegram Mode (`/ytdl tg`) to download a video, generate metadata/thumbnails, and instantly upload it as a streamable file to a linked Telegram channel.
    * Batch queue processing by replying to a `manifest.txt` file.
* **Google Drive Integration (NEW)**:
    * Effortlessly push downloaded files or entire directories to Google Drive directly from the chat.
    * Features highly resilient chunked uploading with real-time UI progress tracking.
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
* **Download Engines**: Aria2 (via JSON-RPC interface) and `yt-dlp` (via native subprocess).
* **Media Processing**: Native `ffmpeg` and `ffprobe` for video metadata and thumbnail extraction.
* **Cloud API**: Google API Client (Drive v3) for chunked, resumable cloud uploads.
* **System Tools**: Native `subprocess`, `shutil`, and `os` modules combined with Linux binaries (`unrar`, `p7zip-full`) for advanced file and archive manipulation.


---


## ⌨️ Command Reference
### 📥 Telegram Downloads (`tgdl`)
* `/mv`, `/mv2`, `/tv`, `/tv2`, `/docu` - Reply to a file to save it to the respective local folder.
* `/lmv <link>`, `/ltv <link>` - Fetch restricted links directly into Movies or TV folders.
* `/search <Channel_ID> (<keywords>)` - Search a channel via Userbot and get a `.json` file list of matches.
  * *(Tip: Reply to the resulting JSON file with `/lmv <Folder_Name>` to bulk download all items!)*


### 🧲 Aria2c Manager (`aria`)
* `/aria <mv|tv|mv2|tv2|docu> <link>` - Send a magnet or direct link to Aria2c.
* `/aria <mv|tv|mv2|tv2|docu>` - Reply to a `.torrent` file to process it.
* `/aria list` - View active, waiting, and stopped downloads.
* `/aria <GID>` - Track the live status of a specific task.
* `/aria <start|stop|rm|del>` - Manage a task (reply to a tracking message).


### 📺 YouTube-DL (`ytdl`)
* `/ytdl <dir_key> <url>` - Download streams/videos to a local directory.
* `/ytdl tg <dir_key> <url>` - Download and automatically upload it to the designated Telegram Channel.
  * *(Tip: Reply to a `manifest.txt` file to batch process multiple URLs!)*


### ☁️ Google Drive (`gd`)
* `/gd` - Reply to a "Download Complete" message to upload that specific file/folder.
* `/gd "<dir_key>/<name>"` - Directly target and upload a specific file or folder (e.g., `/gd "tv/Breaking Bad"`).


### 🗄️ File Manager (`fm`)
* `/fm ls` - List base directories.
* `/fm ls <dir_key/path>` - View contents of a folder.
* `/fm rn "<path>" "<new_name>"` - Rename a file or folder.
* `/fm rn all "<dir>" "<pattern>"` - Bulk rename alphabetically (e.g., `/fm rn all "tv/Show" "S0{NUM:1} E0{NUM:7}.mkv"`).
* `/fm mov "<src>" "<dest>"` - Move a file or folder.
* `/fm rm "<path>"` - Delete a file or folder.


### 🗜️ Archive Management (`unzip`)
* `/unzip` - Reply to a completed download to extract it in place.
* `/unzip del` - Extract the archive and delete the original compressed file.
* `/unzip <dir> <keywords>` - Search a directory for a keyword and extract the matching archive.
* `/unzip del <dir> <keywords>` - Search, extract, and clean up the original file.


### ⚙️ Miscellaneous (`misc`)
* `/cancel` - Reply to a progress message to abort an active Drive upload, Aria task, or Telegram download.
* `/del` - Reply to a completion message to delete that file from disk.
* `/del <dir> <keywords>` - Search a directory by keyword and safely delete matching files.
* `/cls` - Clear all non-pinned messages in the current chat.
* `/cmd <module>` - View detailed help and examples for a specific category.