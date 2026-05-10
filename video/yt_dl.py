import os
import re
import json
import shutil
import logging
import subprocess
import datetime as dt

import yt_dlp

logger = logging.getLogger(__name__)

def get_timestamp():
    """Generates the HHMMSSDDMMYYYY timestamp."""
    return dt.datetime.now().strftime("%H%M%S%d%m%Y")


def sanitize_title(title):
    """Removes invalid characters for safe file naming."""
    return re.sub(r'[\\/*?:"<>|]', "", str(title)).strip()


def parse_manifest(filepath):
    """Parses the comma-separated .txt manifest file."""
    videos = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            parts = [p.strip() for p in line.split(',')]
            url = parts[0]
            title = parts[1] if len(parts) > 1 and parts[1] else None
            start_ts = parts[2] if len(parts) > 2 and parts[2] else None
            end_ts = parts[3] if len(parts) > 3 and parts[3] else None
            videos.append((url, title, start_ts, end_ts))
    return videos


def download_and_process_sync(url, title, start_ts, end_ts, dest_dir, progress_callback=None):
    """Synchronous worker that executes yt-dlp in an isolated temp folder and trims with ffmpeg."""
    timestamp = get_timestamp()
    logger.info(f"Starting YTDL task for URL: {url}")
    
    # 1. Create an isolated temporary directory to trap all M3U8 fragments
    temp_work_dir = os.path.join(dest_dir, f".ytdl_temp_{timestamp}")
    os.makedirs(temp_work_dir, exist_ok=True)
    
    try:
        ydl_opts_info = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
            safe_title = sanitize_title(title if title else info.get('title', 'video'))
    except Exception:
        safe_title = sanitize_title(title if title else "video")
        
    final_filename = f"{safe_title}_{timestamp}.mp4"
    final_path = os.path.join(dest_dir, final_filename)
    
    # Send raw files to the isolated temp folder
    raw_download_path = os.path.join(temp_work_dir, f"raw_{timestamp}.%(ext)s")

    def hook(d):
        if d['status'] == 'downloading' and progress_callback:
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)
            progress_callback(downloaded, total, speed, eta)

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': raw_download_path,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [hook],
        'hls_prefer_native': True # Forces better M3U8 handling
    }
    
    try:
        # 2. Download into temp folder
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the actual merged file in the temp folder
        downloaded_files = os.listdir(temp_work_dir)
        if not downloaded_files:
            raise Exception("yt-dlp failed to produce an output file.")

        downloaded_files.sort(key=lambda x: os.path.getsize(os.path.join(temp_work_dir, x)), reverse=True)
        actual_raw_path = os.path.join(temp_work_dir, downloaded_files[0])

        # 3. Trim or Move to final destination
        if bool(start_ts or end_ts):
            ffmpeg_cmd = ['ffmpeg', '-y', '-i', actual_raw_path]
            if start_ts: ffmpeg_cmd.extend(['-ss', start_ts])
            if end_ts: ffmpeg_cmd.extend(['-to', end_ts])
            ffmpeg_cmd.extend(['-c', 'copy', final_path])
            
            res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(final_path):
                raise Exception(f"FFmpeg trim error: {res.stderr}")
        else:
            shutil.move(actual_raw_path, final_path)
            
        final_size = os.path.getsize(final_path)
        logger.info(f"YTDL Success | Saved: '{safe_title}' | Size: {final_size} bytes | Path: {final_path} | Source: {url}")
        return final_path, safe_title
        
    finally:
        # 4. GUARANTEED CLEANUP: Nuke the temp folder and all fragments inside it
        if os.path.exists(temp_work_dir):
            shutil.rmtree(temp_work_dir, ignore_errors=True)


def get_video_metadata(filepath):
    """Extracts width, height, and duration via ffprobe for Telegram mediainfo."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "json", filepath
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(res.stdout).get("streams", [{}])[0]
        w = int(info.get("width", 1280))
        h = int(info.get("height", 720))
        d = int(float(info.get("duration", 0)))
        return w, h, d
    except Exception:
        return 1280, 720, 0


def generate_thumbnail(filepath):
    """Generates a 1-frame JPG thumbnail at the 2-second mark."""
    thumb_path = filepath.rsplit('.', 1)[0] + '.jpg'
    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-ss", "00:00:02", "-vframes", "1",
        thumb_path
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if os.path.exists(thumb_path):
            return thumb_path
    except Exception:
        pass
    return None


