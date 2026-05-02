import os
import subprocess
from datetime import datetime


class AudioConverter:
    def convert_audio(self, input_file, codec, bitrate="128k", channels="2"):
        """
        Converts an audio file to the specified format using FFmpeg.
        Parameters:
        - input_file: Path to the source audio file.
        - codec: Target audio codec (e.g., 'mp3', 'aac', 'eac3', 'flac', etc.).
        - bitrate: Audio bitrate (default: 128k).
        - channels: Number of audio channels (default: 2).
        """
        # Extract the directory and filename without extension
        dir_name = os.path.dirname(input_file)
        base_name = os.path.basename(input_file)
        file_name, file_ext = os.path.splitext(base_name)
        current_time = datetime.now()
        timestamp = current_time.strftime("%H%M%S_%d%m%Y")
        # Define the output file path
        output_file = os.path.join(dir_name, f"{file_name}_{timestamp}.{codec}")
            
        try:
            command = [
                'ffmpeg',
                '-i', input_file,
                '-c:a', codec,
                '-b:a', bitrate,
                '-ac', channels,
                '-y',  
                output_file
            ]

            subprocess.run(command, check=True)
            print("-" * 50)
            print(f"Conversion successful!! \nINPUT : {input_file} \nOUTPUT : {output_file}")
            print("-" * 50)

        except subprocess.CalledProcessError as e:
            print(f"Error during conversion: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    codec = "eac3"
    bitrate = "224"
    channels = "2"
    input_file = r"E:\HDHub4uTv.mka"
    
    converter = AudioConverter()

    if not os.path.isfile(input_file):
        print(f"Input file does not exist: {input_file}")
    else:
        converter.convert_audio(input_file=input_file, 
                                codec=codec, 
                                bitrate=f"{bitrate}k", 
                                channels=channels)
    
    
    