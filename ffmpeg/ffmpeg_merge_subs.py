import os
import subprocess

def create_output_filename(video_filename):
    base, ext = os.path.splitext(video_filename)
    return f"{base}_with_subs{ext}"

def mux_video_subtitle(video_file, subtitle_file, output_file, subtitle_delay_ms):
    # Construct the ffmpeg command to mux video with subtitle and apply delay
    cmd = [
        'ffmpeg', '-i', video_file, '-itsoffset', str(subtitle_delay_ms / 1000), '-i', subtitle_file,
        '-c', 'copy', '-c:s', 'mov_text', '-map', '0:v', '-map', '0:a?', '-map', '1', output_file
    ]
    
    subprocess.run(cmd)
    print(f"Processed file saved as {output_file}")

def process_files(video_folder, subtitle_folder, subtitle_delay_ms):
    video_files = sorted(os.listdir(video_folder))
    subtitle_files = sorted(os.listdir(subtitle_folder))
    
    for video_file, subtitle_file in zip(video_files, subtitle_files):
        if video_file.endswith('.mkv') and subtitle_file.endswith('.srt'):
            video_path = os.path.join(video_folder, video_file)
            subtitle_path = os.path.join(subtitle_folder, subtitle_file)
            output_file = create_output_filename(video_file)
            output_path = os.path.join(video_folder, output_file)
            
            mux_video_subtitle(video_path, subtitle_path, output_path, subtitle_delay_ms)

# Replace with the paths to your folders containing the MKV files and subtitles
video_folder = '/path/to/your/mkv/files'
subtitle_folder = '/path/to/your/subtitles'
subtitle_delay_ms = -1875

process_files(video_folder, subtitle_folder, subtitle_delay_ms)
