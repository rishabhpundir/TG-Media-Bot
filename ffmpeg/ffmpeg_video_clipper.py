# video_clipper.py
import os
import subprocess
from datetime import datetime


class VideoClipper:
    def clip_video(
        self,
        input_file: str,
        segments: list[tuple[str, str]],
        container: str = "mp4",
    ):
        """
        Quickly extracts multiple clips from a video without re-encoding.

        Parameters
        ----------
        input_file : str
            Path to the source video.
        segments : list[tuple[str, str]]
            List of (start, end) pairs in HH:MM:SS[.mmm] format.
        container : str, optional
            Target container/extension (e.g. "mkv", "mp4", "mov").
        """
        # Split path parts once for reuse
        dir_name = os.path.dirname(input_file)
        base_name = os.path.basename(input_file)
        file_stem, _ = os.path.splitext(base_name)

        # Timestamp for all outputs in this run
        run_stamp = datetime.now().strftime("%H%M%S_%d%m%Y")

        for idx, (start, end) in enumerate(segments, start=1):
            out_name = f"{file_stem}_{run_stamp}_part{idx}.{container}"
            output_file = os.path.join(dir_name, out_name)

            # `-ss`/`-to` placed *after* the input for frame-accurate cuts
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "info",  # keep console clean
                "-i", input_file,
                "-ss", start,
                "-to", end,
                "-c", "copy",          # stream-copy => no quality loss
                "-map", "0",           # copy all streams (audio/subs too)
                "-y",                  # overwrite if file exists
                output_file,
            ]

            try:
                subprocess.run(cmd, check=True)
                print(f"[✔] Saved clip {idx}: {output_file}")
            except subprocess.CalledProcessError as e:
                print(f"[✘] FFmpeg failed for segment {idx}: {e}")
            except Exception as e:
                print(f"[✘] Unexpected error: {e}")


if __name__ == "__main__":
    # ─── Edit these values or wire them up to argparse ───
    input_file = r"/Users/rishabhpundir/Projects/hoskins/engine/Chat-Engine/static/videos/aubri_intro.mp4"
    container = "mp4"
    segments = [
        ("00:00:00", "00:00:08.500"),  # clip 1
        # ("00:10:00", "00:12:15"),  # clip 2
    ]

    clipper = VideoClipper()

    if not os.path.isfile(input_file):
        print(f"Input file does not exist: {input_file}")
    else:
        clipper.clip_video(
            input_file=input_file,
            segments=segments,
            container=container,
        )
