import re
import os


def format_bytes(size):
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"


def sanitize_filename(filename):
    """
    Sanitizes filename. Only removes invalid chars. 
    Preserves spaces and brackets.
    """
    # Remove ONLY invalid filesystem characters
    filename = re.sub(r'[\\/*?:"<>|]', '', filename)
    filename = filename.replace('\n', ' ').replace('\r', '')
    return filename.strip()


def ensure_mkv_extension(filename):
    """Ensures file has an extension, defaulting to .mkv if none exists."""
    _, ext = os.path.splitext(filename)
    # Only append .mkv if there is absolutely no extension present
    if not ext:
        filename += ".mkv"
    return filename


