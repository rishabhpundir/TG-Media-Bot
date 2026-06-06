import os
import sys
import subprocess
import numpy as np
from scipy.signal import correlate

# Replaced hardcoded wildcard search with sys.argv for console arguments
if len(sys.argv) != 3:
    # Errors are sent to stderr so they don't corrupt the pure ms output on stdout
    sys.stderr.write("Usage: python find_delay.py <file1.mkv> <file2.mkv>\n")
    sys.exit(1)

file_1 = sys.argv[1]
file_2 = sys.argv[2]

wav_1 = "temp_1.wav"
wav_2 = "temp_2.wav"

# Updated ffmpeg input variables and fixed the stream mapping
subprocess.run(['ffmpeg', '-y', '-ss', '00:10:00', '-t', '00:05:00', '-i', file_1, '-map', '0:a:0', '-ar', '16000', '-ac', '1', wav_1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['ffmpeg', '-y', '-ss', '00:10:00', '-t', '00:05:00', '-i', file_2, '-map', '0:a:0', '-ar', '16000', '-ac', '1', wav_2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

# Converted the delay to milliseconds and removed the muxing logic
delay_ms = int(round((lag / 16000.0) * 1000))

# Print strictly the millisecond value with the 'ms' suffix
print(f"Delay: {delay_ms}ms")


