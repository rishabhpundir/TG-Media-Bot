import os
import sys
import subprocess
import numpy as np
from scipy.signal import correlate

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if len(sys.argv) != 3:
    sys.stderr.write("Usage: python find_delay.py <file1.mkv> <file2.mkv>\n")
    sys.exit(1)

# Convert to absolute paths so it works no matter where you run it from
file_1 = os.path.abspath(sys.argv[1])
file_2 = os.path.abspath(sys.argv[2])

# Pre-flight check: Do these files actually exist?
if not os.path.isfile(file_1):
    sys.stderr.write(f"Error: Cannot find File 1 at '{file_1}'\n")
    sys.exit(1)
if not os.path.isfile(file_2):
    sys.stderr.write(f"Error: Cannot find File 2 at '{file_2}'\n")
    sys.exit(1)

wav_1 = os.path.join(SCRIPT_DIR, "temp_1.wav")
wav_2 = os.path.join(SCRIPT_DIR, "temp_2.wav")

def extract_audio(input_file, output_wav):
    # We now capture stderr instead of DEVNULL. If it fails, we can print the exact reason.
    result = subprocess.run(
        ['ffmpeg', '-y', '-ss', '00:10:00', '-i', input_file, '-t', '00:05:00', 
         '-map', '0:a:0', '-vn', '-sn', '-dn', '-ar', '16000', '-ac', '1', output_wav], 
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        sys.stderr.write(f"FFmpeg failed to extract audio from '{input_file}'.\nFFmpeg Error:\n{result.stderr}\n")
        sys.exit(1)

# Extract both files
extract_audio(file_1, wav_1)
extract_audio(file_2, wav_2)

def load_wav(filename):
    with open(filename, 'rb') as f:
        f.seek(44)
        return np.fromfile(f, dtype=np.int16)

audio_1 = load_wav(wav_1)
audio_2 = load_wav(wav_2)

os.remove(wav_1)
os.remove(wav_2)

# Calculate cross-correlation to find where they match up
correlation = correlate(audio_1, audio_2, mode='full')
lag = np.argmax(correlation) - (len(audio_2) - 1)
delay_ms = int(round((lag / 16000.0) * 1000))

# Print strictly the millisecond value
print(f"{delay_ms}ms")


