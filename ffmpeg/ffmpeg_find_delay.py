#!/usr/bin/env python3
"""
Find the audio delay of file_2 relative to file_1 via cross-correlation.

Usage:
    python ffmpeg_find_delay.py <file1> <file2>
        [--start HH:MM:SS] [--dur SECONDS] [--rate HZ]

Notes:
  * Cross-correlation aligns the SHARED parts of the audio (music / sound
    effects). It works even when the two files are different language dubs,
    as long as they share the same M&E timeline. Check the printed
    "confidence" value -- a low one means the result is not trustworthy
    (e.g. different runtimes, PAL speedup, or genuinely unrelated audio).
"""

import os
import sys
import time
import argparse
import subprocess
import numpy as np
from scipy.signal import correlate, correlation_lags
from scipy.io import wavfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    """Timestamped progress that is actually flushed so you see it live."""
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def parse_args():
    p = argparse.ArgumentParser(description="Find audio delay of file2 vs file1.")
    p.add_argument("file1")
    p.add_argument("file2")
    p.add_argument("--start", default="00:10:00",
                   help="Where to start sampling (default 00:10:00).")
    p.add_argument("--dur", type=int, default=120,
                   help="Seconds of audio to analyze (default 120). "
                        "Shorter = faster; must exceed the expected delay.")
    p.add_argument("--rate", type=int, default=16000,
                   help="Resample rate in Hz (default 16000).")
    p.add_argument("--a1", type=int, default=0,
                   help="Audio stream index for file1 (default 0).")
    p.add_argument("--a2", type=int, default=0,
                   help="Audio stream index for file2 (default 0).")
    return p.parse_args()


def extract_audio(input_file, output_wav, start, dur, rate, stream_idx, label):
    log(f"{label}: extracting {dur}s of audio stream {stream_idx} from {start} ...")
    t0 = time.time()
    # -ss BEFORE -i = fast input seek. -nostdin avoids stdin stalls.
    # If THIS step is the slow one, the file's seek index is bad/missing.
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-ss", start, "-i", input_file, "-t", str(dur),
         "-map", f"0:a:{stream_idx}", "-vn", "-sn", "-dn",
         "-ar", str(rate), "-ac", "1", output_wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        log(f"FFmpeg failed on '{input_file}':\n{result.stderr}")
        sys.exit(1)
    log(f"{label}: done in {time.time() - t0:.1f}s")


def load_audio(path):
    # Parses the WAV header correctly (no fragile seek(44)) and returns float.
    rate, data = wavfile.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)         # <-- THE FIX: float prevents overflow
    data -= data.mean()                    # remove DC offset
    std = data.std()
    if std > 0:
        data /= std                        # normalize for a comparable confidence
    return rate, data


def main():
    args = parse_args()
    file_1 = os.path.abspath(args.file1)
    file_2 = os.path.abspath(args.file2)
    for f, name in [(file_1, "File 1"), (file_2, "File 2")]:
        if not os.path.isfile(f):
            log(f"Error: cannot find {name} at '{f}'")
            sys.exit(1)

    wav_1 = os.path.join(SCRIPT_DIR, "temp_1.wav")
    wav_2 = os.path.join(SCRIPT_DIR, "temp_2.wav")

    extract_audio(file_1, wav_1, args.start, args.dur, args.rate, args.a1, "File 1")
    extract_audio(file_2, wav_2, args.start, args.dur, args.rate, args.a2, "File 2")

    log("Loading audio into RAM...")
    rate_1, audio_1 = load_audio(wav_1)
    rate_2, audio_2 = load_audio(wav_2)
    os.remove(wav_1)
    os.remove(wav_2)
    rate = rate_1  # both forced to args.rate above

    log(f"Correlating ({len(audio_1):,} x {len(audio_2):,} samples, FFT)...")
    t0 = time.time()
    corr = correlate(audio_1, audio_2, mode="full", method="fft")
    lags = correlation_lags(len(audio_1), len(audio_2), mode="full")
    peak_idx = int(np.argmax(np.abs(corr)))
    lag = int(lags[peak_idx])
    log(f"Correlation done in {time.time() - t0:.1f}s")

    # Confidence: how far the peak stands above the background noise.
    peak = abs(corr[peak_idx])
    confidence = (peak - np.mean(np.abs(corr))) / (np.std(np.abs(corr)) + 1e-9)

    delay_ms = int(round((lag / float(rate)) * 1000))
    log(f"confidence (peak-to-sidelobe): {confidence:.1f}  "
        f"(>~10 = reliable, <~5 = suspect)")
    # Pure output on stdout:
    print(f"{delay_ms}ms")


if __name__ == "__main__":
    main()
    
    
    