import os
import re
import json
import subprocess
import datetime as dt
import yt_dlp

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

def download_and_process_sync(url, title, start_ts, end_ts, dest_dir):
    """Synchronous worker that executes yt-dlp and trims with ffmpeg."""
    timestamp = get_timestamp()
    
    # Extract info to get the title safely
    ydl_opts_info = {'quiet': True, 'no_warnings': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
            safe_title = sanitize_title(title if title else info.get('title', 'video'))
    except Exception:
        safe_title = sanitize_title(title if title else f"video")
        
    final_filename = f"{safe_title}_{timestamp}.mp4"
    final_path = os.path.join(dest_dir, final_filename)
    
    needs_trim = bool(start_ts or end_ts)
    download_path = os.path.join(dest_dir, f"temp_{timestamp}.mp4") if needs_trim else final_path

    # Configure download (best video + best audio merged to mp4)
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': download_path,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    if not os.path.exists(download_path):
        raise Exception("yt-dlp failed to produce output file.")

    # Execute FFmpeg Trim if required
    if needs_trim:
        ffmpeg_cmd = ['ffmpeg', '-y', '-i', download_path]
        if start_ts:
            ffmpeg_cmd.extend(['-ss', start_ts])
        if end_ts:
            ffmpeg_cmd.extend(['-to', end_ts])
        ffmpeg_cmd.extend(['-c', 'copy', final_path])
        
        res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if os.path.exists(download_path):
            os.remove(download_path)
            
        if res.returncode != 0 or not os.path.exists(final_path):
            raise Exception(f"FFmpeg trim error: {res.stderr}")
            
    return final_path, safe_title

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


