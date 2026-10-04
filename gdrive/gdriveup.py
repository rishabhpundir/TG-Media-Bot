import os
import sys
import json
import time
import socket
import logging
import http.client

from tqdm import tqdm
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from googleapiclient.http import MediaFileUpload

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import ledger_db
from gdrive.auth import get_service

# --- LOGGING SETUP ---
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'gdrive_log.log')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=1, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# Ensure it loads .env
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

SCOPES = ['https://www.googleapis.com/auth/drive.file']
socket.setdefaulttimeout(300)

TARGET_DRIVE_FOLDER_ID = os.getenv('TARGET_DRIVE_FOLDER_ID')
LIST_FILE_NAME = 'upload.txt'

# Ledger is namespaced per Drive folder, otherwise movies/X.mkv and tv/X.mkv
# share a ledger key and the second upload gets silently skipped.
LEDGER_ROOT_KEY = 'root'

# Lock credentials to the script's directory
TOKEN_PATH = os.path.join(SCRIPT_DIR, 'token.json')
CREDS_PATH = os.path.join(SCRIPT_DIR, 'credentials.json')


# SQLite Ledger (see ledger_db.py)
def get_size_bytes(path):
    """Size of a file in bytes. Folders return None -- we no longer walk the
    whole subtree just to record a number nothing reads."""
    try:
        return os.path.getsize(path) if os.path.isfile(path) else None
    except OSError:
        return None


def check_ledger(full_path, parent_id):
    """Drive id if this exact absolute path was already uploaded into this
    exact Drive folder, else None. Zero API calls."""
    return ledger_db.get_upload(full_path, parent_id)


def update_ledger(full_path, parent_id, gid, is_folder):
    """Record an upload against (absolute path, Drive parent folder)."""
    ledger_db.put_upload(
        path=full_path,
        parent_id=parent_id,
        gid=gid,
        is_folder=is_folder,
        size_bytes=get_size_bytes(full_path),
    )


# Google Drive Interaction
def authenticate():
    """Handles OAuth 2.0 authentication with Google Drive."""
    return get_service(SCOPES, token_path=TOKEN_PATH, creds_path=CREDS_PATH)

def get_existing_item(service, name, parent_id, is_folder=False):
    """Searches for an existing file or folder by name within a specific parent."""
    safe_name = name.replace("'", "\\'")
    
    mime_query = "mimeType='application/vnd.google-apps.folder'" if is_folder else "mimeType!='application/vnd.google-apps.folder'"
    query = f"name='{safe_name}' and '{parent_id}' in parents and {mime_query} and trashed=false"
    
    # ADD num_retries=5 HERE to protect the metadata query
    response = service.files().list(
        q=query,
        spaces='drive',
        fields='files(id, name)',
        pageSize=1
    ).execute(num_retries=5) 
    
    files = response.get('files', [])
    if files:
        return files[0].get('id')
    return None


def create_drive_folder(service, dir_path, parent_id):
    """Creates a folder in Google Drive or returns the ID if it already exists."""
    folder_name = os.path.basename(dir_path)
    
    # 1. Check local ledger first (Zero API calls)
    ledger_gid = check_ledger(dir_path, parent_id)
    if ledger_gid:
        return ledger_gid

    # 2. Check Drive API if not in ledger
    existing_folder_id = get_existing_item(service, folder_name, parent_id, is_folder=True)
    if existing_folder_id:
        update_ledger(dir_path, parent_id, existing_folder_id, True)
        return existing_folder_id

    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    
    folder = service.files().create(
        body=file_metadata, 
        fields='id'
    ).execute(num_retries=5)
    
    folder_id = folder.get('id')
    update_ledger(dir_path, parent_id, folder_id, True)
    return folder_id


def upload_file(service, file_path, parent_id, progress_callback=None, cancel_flag=None):
    """Uploads a single file to a specific Google Drive folder, skipping if it exists."""
    file_name = os.path.basename(file_path)
    
    # 1. Check local ledger first (Zero API calls)
    ledger_gid = check_ledger(file_path, parent_id)
    if ledger_gid:
        print(f"Skipping: '{file_name}' (Found in local Ledger)")
        logger.info(f"Skipping: '{file_name}' (Found in local Ledger)")
        return ledger_gid
    
    # 2. Check if file already exists in this specific Drive folder
    existing_file_id = get_existing_item(service, file_name, parent_id, is_folder=False)
    if existing_file_id:
        print(f"Skipping: '{file_name}' (Already exists in Drive)")
        logger.info(f"Skipping: '{file_name}' (Already exists in Drive)")
        update_ledger(file_path, parent_id, existing_file_id, False)
        return existing_file_id

    file_metadata = {'name': file_name, 'parents': [parent_id]}
    file_size = os.path.getsize(file_path)
    
    chunk_size = 10 * 1024 * 1024 
    media = MediaFileUpload(file_path, chunksize=chunk_size, resumable=True)
    
    request = service.files().create(body=file_metadata, media_body=media, fields='id')
    
    response = None
    
    print(f"\nUploading: {file_name}")
    logger.info(f"Started uploading: {file_name} ({file_size} bytes)")
    with tqdm(total=file_size, unit='B', unit_scale=True, unit_divisor=1024) as pbar:
        while response is None:
            # --- CHECK CANCELLATION FLAG BEFORE NEXT CHUNK ---
            if cancel_flag and cancel_flag.get("cancelled"):
                logger.info(f"Upload forcibly aborted via cancel flag: {file_name}")
                raise Exception("Upload Cancelled")
                
            try:
                status, response = request.next_chunk(num_retries=5)
                if status:
                    pbar.update(status.resumable_progress - pbar.n)
                    if progress_callback:
                        progress_callback(status.resumable_progress, file_size, file_name)
                    
            except (TimeoutError, socket.timeout, http.client.HTTPException) as e:
                tqdm.write(f"\nNetwork hiccup detected: {e}. Retrying chunk in 5 seconds...")
                logger.warning(f"Network hiccup during {file_name}: {e}. Retrying...")
                time.sleep(5)
                
    file_id = response.get('id')
    logger.info(f"Successfully uploaded: {file_name}")
    update_ledger(file_path, parent_id, file_id, False)
    return file_id


def upload_directory(service, dir_path, parent_id, progress_callback=None, cancel_flag=None):
    """Recursively uploads a directory and its contents to Google Drive."""
    if cancel_flag and cancel_flag.get("cancelled"):
        raise Exception("Upload Cancelled")
        
    dir_name = os.path.basename(dir_path)
    print(f"Creating/Checking Drive folder: {dir_name}...")
    logger.info(f"Processing directory: {dir_name}")
    
    drive_folder_id = create_drive_folder(service, dir_path, parent_id)
    
    for item in os.listdir(dir_path):
        if cancel_flag and cancel_flag.get("cancelled"):
            raise Exception("Upload Cancelled")
            
        item_path = os.path.join(dir_path, item)
        if os.path.isfile(item_path):
            upload_file(service, item_path, drive_folder_id, progress_callback, cancel_flag)
        elif os.path.isdir(item_path):
            upload_directory(service, item_path, drive_folder_id, progress_callback, cancel_flag)


def upload_single_target(target_path, progress_callback=None, cancel_flag=None,
                         folder_id=None, base_dir=None):
    """Entry point for the Telegram bot to upload a specific file/folder.

    folder_id : Drive destination (movies / tv). Falls back to the legacy folder.
    base_dir  : accepted and ignored. The SQLite ledger keys on absolute paths,
                so there is no relative-path base to establish any more. Kept
                so existing callers do not need changing.
    """
    dest_folder_id = folder_id or TARGET_DRIVE_FOLDER_ID
    if not dest_folder_id:
        raise Exception("No Drive folder resolved. Set MOVIES_DRIVE_FOLDER_ID / "
                        "TV_DRIVE_FOLDER_ID (or TARGET_DRIVE_FOLDER_ID) in .env")

    target_path = os.path.abspath(target_path)
    if not os.path.exists(target_path):
        raise Exception(f"Path does not exist on disk: {target_path}")

    logger.info(f"Bot triggered Drive upload for: {target_path} -> folder {dest_folder_id}")
    service = authenticate()

    if os.path.isfile(target_path):
        upload_file(service, target_path, dest_folder_id, progress_callback, cancel_flag)
    elif os.path.isdir(target_path):
        upload_directory(service, target_path, dest_folder_id, progress_callback, cancel_flag)


def main():
    global BASE_DIR
    
    if not TARGET_DRIVE_FOLDER_ID:
        error_msg = "Error: TARGET_DRIVE_FOLDER_ID is not set. Check your .env file."
        print(error_msg)
        logger.error(error_msg)
        sys.exit(1)
            
    if len(sys.argv) < 2:
        print("Usage: python script.py <base_directory>")
        sys.exit(1)

    raw_base_dir = sys.argv[1]
    if raw_base_dir.endswith('/'):
        raw_base_dir = raw_base_dir[:-1]
    BASE_DIR = os.path.abspath(raw_base_dir)
        
    list_file_path = os.path.join(BASE_DIR, LIST_FILE_NAME)

    if not os.path.exists(list_file_path):
        error_msg = f"Error: Could not find '{LIST_FILE_NAME}' in {BASE_DIR}"
        print(error_msg)
        logger.error(error_msg)
        sys.exit(1)

    print("Authenticating with Google Drive...")
    logger.info(f"Initializing upload job from base directory: {BASE_DIR}")
    service = authenticate()

    with open(list_file_path, 'r') as f:
        items = [line.strip().strip("\"'") for line in f.readlines() if line.strip()]

    for item in items:
        full_path = os.path.join(BASE_DIR, item)
        
        if os.path.isfile(full_path):
            upload_file(service, full_path, TARGET_DRIVE_FOLDER_ID)
        elif os.path.isdir(full_path):
            upload_directory(service, full_path, TARGET_DRIVE_FOLDER_ID)
        else:
            warning_msg = f"Warning: '{full_path}' does not exist on disk. Skipping."
            print(warning_msg)
            logger.warning(warning_msg)
            
    print("\nUpload process complete!")
    logger.info("Upload process fully completed.")


if __name__ == '__main__':
    main()
    
    
    