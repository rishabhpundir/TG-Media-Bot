"""
Automated MP3 Downloader & FFmpeg Converter

This script interfaces with a local music API to download high-quality (320kbps) audio tracks,
converts them to universally playable MP3s via FFmpeg, and maintains a local JSON ledger of downloads.
It includes intelligent filtering to skip renditions (lofi/covers/remixes) and defaults to original tracks.

REQUIREMENTS:
To run this script in batch mode, you must have a text file (default: 'songs_list.txt') where 
each line contains a single song name or lyric snippet (e.g., "Tum Hi Ho - Aashiqui 2 (2013)"). 
The script will actively update this file with '✅' (Success) or '⚠️' (Failed/Not Found) markers.

USAGE EXAMPLES:

1. Batch Download Mode (Using a text file)
   Run the jiosaavn API in a terminal:
   $ python api/app.py
   Then, in another terminal, run the script to process the default 'songs_list.txt' file:
   $ python saavn_dl.py
   
   To use a custom text file, use the -f or --file flag:
   $ python saavn_dl.py -f my_custom_playlist.txt

2. Direct Single Query Mode (Using argparse)
   Pass a specific song name directly in the terminal to bypass the text file 
   and download just that single track:
   $ python saavn_dl.py "Tum Hi Ho (2013)"
"""

import re
import os
import sys
import html
import json
import time
import shutil
import random
import logging
import argparse
import requests
import subprocess

from pathlib import Path
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = "output"
LEDGER_FILE = "output/saavn_download_ledger.json"
TEMP_DIR = "temp"
API_BASE_URL = os.getenv("MUSIC_API_BASE_URL")
MP3_CONVERT = os.getenv("MUSIC_MP3_CONVERT", "true").lower() == "true"

# Ensure directories exist
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)

# --- Log Configuration ---
MUSIC_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'logs')
os.makedirs(MUSIC_LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(MUSIC_LOG_DIR, 'music_logs.log')

logging.basicConfig(
    level=logging.INFO, # Change to logging.DEBUG for deeper troubleshooting
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        # Max file size of 5MB. Keeps exactly 1 older backup file.
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=1, encoding="utf-8"), 
        logging.StreamHandler(sys.stdout) # Prints to console
    ]
)

logger = logging.getLogger(__name__)


def load_ledger():
    """Loads the progress ledger to resume interrupted jobs."""
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_to_ledger(ledger_data):
    """Saves the current state to the JSON ledger."""
    with open(LEDGER_FILE, 'w', encoding='utf-8') as f:
        json.dump(ledger_data, f, indent=4, ensure_ascii=False)

def sanitize_filename(filename):
    """Unescapes HTML entities and removes illegal characters for safe filesystem writing."""
    decoded = html.unescape(filename)
    return re.sub(r'[\\/*?:"<>|]', "", decoded).strip()

def format_duration(seconds):
    """Converts seconds to HH:MM:SS format."""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"

def get_best_audio_url(media_url):
    """Ensures the media URL is pushed to the highest 320kbps bitrate."""
    if not media_url:
        return None
    # The local API gives a direct CDN link. We force the suffix to _320.mp4 for max quality.
    return re.sub(r'_\d+\.mp4$', '_320.mp4', media_url)


def download_and_convert(url, final_filepath):
    """Downloads the raw stream and converts it to a standard 320kbps MP3 via FFmpeg."""
    temp_filepath = os.path.join(TEMP_DIR, "temp_stream.m4a")
    
    # 1. Download the raw file
    response = requests.get(url, stream=True)
    response.raise_for_status()
    with open(temp_filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            
    # 2. Convert via FFmpeg OR keep original
    if MP3_CONVERT:
        command = [
            'ffmpeg', '-y',        # Overwrite output files
            '-i', temp_filepath,   # Input file
            '-vn',                 # Disable video stream (if any embedded covers exist)
            '-c:a', 'libmp3lame',  # Specify MP3 encoder
            '-b:a', '320k',        # Constant Bitrate (CBR) at 320kbps
            '-ar', '44100',        # Set sample rate to 44.1 kHz
            final_filepath
        ]
        
        # Run FFmpeg silently
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        # 3. Clean up the temporary raw file
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)
    else:
        # Bypass FFmpeg and just move the raw downloaded file to the final destination
        shutil.move(temp_filepath, final_filepath)
        
    # Return file size in MB
    return round(os.path.getsize(final_filepath) / (1024 * 1024), 2)

def update_line_status(file_path, original_query, status_marker):
    """Appends a status marker to a specific line in the text file."""
    if not file_path or not os.path.exists(file_path): 
        return
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    with open(file_path, "w", encoding="utf-8") as f:
        for line in lines:
            # Append marker only to the exact unmatched query line to avoid double tagging
            if line.strip() == original_query:
                f.write(f"{status_marker} {line.rstrip()}\n")
            else:
                f.write(line)

def get_original_song(results, query):
    """Filters out renditions and finds the original song by year matching."""
    # Attempt to extract a target year from the query (e.g. "(1981)")
    target_year_match = re.search(r'\((\d{4})\)', query)
    target_year = int(target_year_match.group(1)) if target_year_match else None
    
    banned_words = ['lofi', 'lo-fi', 'remix', 'cover', 'dj']
    valid_songs = []
    
    for song in results:
        title = song.get('song', '').lower()
        
        # 1. Skip Renditions
        if any(word in title for word in banned_words):
            continue
        
        # 2. Extract the year from the local API's payload
        song_year_match = re.search(r'\d{4}', str(song.get('release_date', '')))
        if not song_year_match:
            song_year_match = re.search(r'\d{4}', str(song.get('copyright_text', '')))
        
        song_year = int(song_year_match.group()) if song_year_match else 9999
        valid_songs.append((song_year, song))
        
    if not valid_songs:
        return None
        
    # Sort valid songs by oldest year first to default to the original track
    valid_songs.sort(key=lambda x: x[0])
    
    # 3. Match the specific year if it was provided in the query string
    if target_year:
        for year, song in valid_songs:
            if year == target_year:
                return song
                
    # Default fallback: return the oldest remaining valid song
    return valid_songs[0][1]

def process_songs(queries, list_file_path=None):
    ledger = load_ledger()
    download_count = 0

    for query in queries:
        # Check Ledger to skip already downloaded songs
        if query in ledger:
            logger.info(f"⏭️ Skipping '{query}' - Already in ledger.")
            continue
            
        logger.info(f"🔍 Searching for: {query}")
        
        try:
            res = requests.get(f"{API_BASE_URL}?query={requests.utils.quote(query)}")
            res.raise_for_status()
            data = res.json()
            
            # The local API returns a list directly on success
            results = data if isinstance(data, list) else []
            
            # Fallback Mechanism: If no results, it might be a long lyric line.
            # Retry the search using just the first 4 words to broaden the hit rate.
            if not results and len(query.split()) > 4:
                fallback_query = " ".join(query.split()[:4])
                logger.warning(f"⚠️ No exact match. Retrying with lyric fallback: '{fallback_query}'")
                res_fallback = requests.get(f"{API_BASE_URL}?query={requests.utils.quote(fallback_query)}")
                res_fallback.raise_for_status()
                fallback_data = res_fallback.json()
                results = fallback_data if isinstance(fallback_data, list) else []

            if results:
                # Select the best matching original song
                song = get_original_song(results, query)
                
                if not song:
                    logger.info(f"❌ Original song not found for '{query}' (all results were renditions/remixes).")
                    if list_file_path:
                        update_line_status(list_file_path, query, "⚠️ -")
                    continue
                
                # Extract Metadata mapped to the local API's keys
                title = song.get('song', 'Unknown Title')
                year = song.get('year', 'Unknown Year')
                album = song.get('album', 'Unknown Album')
                
                try:
                    # Duration comes as a string in this API (e.g., "262")
                    duration_sec = int(song.get('duration', 0))
                except ValueError:
                    duration_sec = 0
                
                # Extract Primary Artists
                singers = song.get('primary_artists', 'Unknown Artist')
                
                # Get correct download link
                best_url = get_best_audio_url(song.get('media_url'))
                
                if not best_url:
                    logger.warning(f"⚠️ No downloadable stream found for '{query}'.")
                    continue
                
                # Format Filename
                safe_title = sanitize_filename(title)
                safe_singers = sanitize_filename(singers)
                safe_album = sanitize_filename(album)
                
                # Assign extension based on .env configuration
                ext = ".mp3" if MP3_CONVERT else ".m4a"
                filename = f"{safe_title} - {safe_singers} - {safe_album} ({year}){ext}"
                filepath = os.path.join(OUTPUT_DIR, filename)
                
                logger.info(f"⬇️ Downloading: {filename}")
                file_size_mb = download_and_convert(best_url, filepath)
                
                # Update Ledger
                ledger[query] = {
                    "song_name": title,
                    "album": album,
                    "singers": singers,
                    "year": year,
                    "size_mb": file_size_mb,
                    "length": format_duration(duration_sec),
                    "original_download_url": best_url
                }
                save_to_ledger(ledger)
                logger.info(f"✅ Success. Saved to ledger.")
                
                # Mark the line as successful in the list file to track dual-progress
                if list_file_path:
                    update_line_status(list_file_path, query, "✅ -")
                
                download_count += 1
                
                # Break architecture
                if download_count % 10 == 0:
                    logger.info("🛑 10 downloads reached. Taking a 30-second cooling break...")
                    time.sleep(30)
                else:
                    # Randomize sleep between 5 and 10 seconds to avoid pattern detection
                    sleep_time = random.uniform(5, 10)
                    logger.info(f"⏳ Sleeping for {sleep_time:.2f} seconds...")
                    time.sleep(sleep_time)

            else:
                logger.info(f"❌ No results found for '{query}'.")
                if list_file_path:
                    update_line_status(list_file_path, query, "⚠️ -")
                
        except Exception as e:
            logger.exception(f"⚠️ Error processing '{query}': {e}")
            if list_file_path:
                update_line_status(list_file_path, query, "⚠️ -")
        
            
if __name__ == "__main__":
    # Ensure the local API is alive before booting up
    try:
        # A simple connection test to the host URL
        requests.get(API_BASE_URL, timeout=3)
    except requests.exceptions.RequestException:
        print("❌ API is not running. Please start the API by running 'python music/api/app.py'")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Automated MP3 Downloader & FFmpeg Converter")
    parser.add_argument(
        "query", 
        nargs="?", 
        help="A single song name or lyric to download directly (e.g., \"Tum hi ho\")"
    )
    parser.add_argument(
        "-f", "--file", 
        default="music/songs_list.txt", 
        help="Path to the text file containing the list of songs (defaults to songs_list.txt)"
    )
    
    args = parser.parse_args()

    if args.query:
        logger.info(f"🚀 Starting direct download for: '{args.query}'")
        process_songs([args.query])
        logger.info("🎉 Download finished.")
    else:
        if os.path.exists(args.file):
            logger.info(f"🚀 Starting batch download pipeline from '{args.file}'...")
            queries_list = []
            with open(args.file, 'r', encoding='utf-8') as f:
                # Safely parse the file, dropping empty lines and source markers (e.g., )
                queries_list = [line.strip() for line in f if line.strip() and not line.startswith(' ')]
                
            process_songs(queries_list, list_file_path=args.file)
            logger.info("🎉 Pipeline finished.")
        else:
            logger.exception(f"❌ Error: Target file '{args.file}' not found, and no direct query provided.")
        
        
        