
import os
import subprocess
import json

def get_streams_info(file_path):
    """Get streams information from the MKV file using ffprobe."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'stream=index,codec_type,codec_name,language,bit_rate', '-of', 'json', file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    info = json.loads(result.stdout)
    return info['streams']

def create_output_filename(input_filename):
    base, ext = os.path.splitext(input_filename)
    return f"{base} ~ NEW{ext}"

def remove_foreign_audio(file_path):
    streams = get_streams_info(file_path)
    
    video_stream = None
    english_audio_stream = None
    english_subtitle_stream = None

    for stream in streams:
        if stream['codec_type'] == 'video':
            video_stream = stream['index']
        elif stream['codec_type'] == 'audio' and stream.get('bit_rate') == '224000':
            english_audio_stream = stream['index']
        elif stream['codec_type'] == 'subtitle':
            english_subtitle_stream = stream['index']

    output_file = create_output_filename(file_path)

    # Construct the ffmpeg command to keep only the required streams
    cmd = [
        'ffmpeg', '-i', file_path, 
        '-map', f'0:{video_stream}', 
        '-map', f'0:{english_audio_stream}', 
        '-map', f'0:{english_subtitle_stream}', 
        '-c', 'copy', output_file
    ]
    
    subprocess.run(cmd)
    print(f"Processed file saved as {output_file}")

def process_folder(folder_path):
    for filename in os.listdir(folder_path):
        if filename.endswith('.mkv'):
            file_path = os.path.join(folder_path, filename)
            remove_foreign_audio(file_path)

# Replace with the path to your folder containing the MKV files
folder_path = r'D:\Watching Stuff\Movies\The Mask Animated Series (1995-1997)\Season 2 (Web-DL)'
process_folder(folder_path)