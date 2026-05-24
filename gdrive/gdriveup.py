import os
import sys
import json
import time
import socket
import logging
import http.client

from tqdm import tqdm
from dotenv import load_dotenv
from googleapiclient.discovery import build
from logging.handlers import RotatingFileHandler
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# --- LOGGING SETUP ---
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

BASE_DIR = None
LEDGER_PATH = os.path.join(SCRIPT_DIR, 'ledger.json')
TARGET_DRIVE_FOLDER_ID = os.getenv('TARGET_DRIVE_FOLDER_ID')
LIST_FILE_NAME = 'upload.txt'

# Lock credentials to the script's directory
TOKEN_PATH = os.path.join(SCRIPT_DIR, 'token.json')
CREDS_PATH = os.path.join(SCRIPT_DIR, 'credentials.json')


# JSON Ledger
def get_size_in_gb(path):
    """Calculates size of file or folder in GB."""
    total_size = 0
    if os.path.isfile(path):
        total_size = os.path.getsize(path)
    elif os.path.isdir(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
    return f"{total_size / (1024**3):.4f} GB"

def load_ledger():
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"srn_counter": 0, "root": {}}

def save_ledger(ledger_data):
    with open(LEDGER_PATH, 'w') as f:
        json.dump(ledger_data, f, indent=4)

def check_ledger(full_path):
    """Checks if a path is fully uploaded in the ledger."""
    if not BASE_DIR: return None
    ledger = load_ledger()
    rel_path = os.path.relpath(full_path, BASE_DIR)
    parts = rel_path.split(os.sep)
    
    current = ledger["root"]
    for i, part in enumerate(parts):
        if part not in current:
            return None
        if i == len(parts) - 1:
            item = current[part]
            if item.get("uploaded") and item.get("gid"):
                return item["gid"]
            return None
        current = current[part].get("contents", {})
    return None

def update_ledger(full_path, gid, is_folder):
    """Updates the ledger with the nested folder/file structure."""
    if not BASE_DIR: return
    ledger = load_ledger()
    rel_path = os.path.relpath(full_path, BASE_DIR)
    parts = rel_path.split(os.sep)
    
    current = ledger["root"]
    for i, part in enumerate(parts):
        if part not in current:
            ledger["srn_counter"] += 1
            current[part] = {
                "srn": ledger["srn_counter"],
                "name": part,
                "gid": gid if i == len(parts) - 1 else None,
                "uploaded": True if i == len(parts) - 1 else False,
                "size": get_size_in_gb(full_path) if i == len(parts) - 1 else "0 GB",
            }
            if is_folder or i < len(parts) - 1:
                current[part]["contents"] = {}
        else:
            # Update target item
            if i == len(parts) - 1:
                current[part]["gid"] = gid
                current[part]["uploaded"] = True
                current[part]["size"] = get_size_in_gb(full_path)
        
        # Traverse deeper
        if "contents" not in current[part]:
            current[part]["contents"] = {}
        current = current[part]["contents"]
        
    save_ledger(ledger)
    

# Google Drive Interaction
def authenticate():
    """Handles OAuth 2.0 authentication with Google Drive."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)


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
    ledger_gid = check_ledger(dir_path)
    if ledger_gid:
        return ledger_gid

    # 2. Check Drive API if not in ledger
    existing_folder_id = get_existing_item(service, folder_name, parent_id, is_folder=True)
    if existing_folder_id:
        update_ledger(dir_path, existing_folder_id, is_folder=True)
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
    update_ledger(dir_path, folder_id, is_folder=True)
    return folder_id


def upload_file(service, file_path, parent_id, progress_callback=None, cancel_flag=None):
    """Uploads a single file to a specific Google Drive folder, skipping if it exists."""
    file_name = os.path.basename(file_path)
    
    # 1. Check local ledger first (Zero API calls)
    ledger_gid = check_ledger(file_path)
    if ledger_gid:
        print(f"Skipping: '{file_name}' (Found in local Ledger)")
        logger.info(f"Skipping: '{file_name}' (Found in local Ledger)")
        return ledger_gid
    
    # 2. Check if file already exists in this specific Drive folder
    existing_file_id = get_existing_item(service, file_name, parent_id, is_folder=False)
    if existing_file_id:
        print(f"Skipping: '{file_name}' (Already exists in Drive)")
        logger.info(f"Skipping: '{file_name}' (Already exists in Drive)")
        update_ledger(file_path, existing_file_id, is_folder=False)
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
    update_ledger(file_path, file_id, is_folder=False)
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


def upload_single_target(target_path, progress_callback=None, cancel_flag=None):
    """Entry point for the Telegram bot to upload a specific file/folder."""
    global BASE_DIR
    
    if not TARGET_DRIVE_FOLDER_ID:
        raise Exception("TARGET_DRIVE_FOLDER_ID is not set in .env")
        
    target_path = os.path.abspath(target_path)
    if not os.path.exists(target_path):
        raise Exception(f"Path does not exist on disk: {target_path}")

    BASE_DIR = os.path.dirname(target_path)
    
    logger.info(f"Bot triggered Drive upload for: {target_path}")
    service = authenticate()
    
    if os.path.isfile(target_path):
        upload_file(service, target_path, TARGET_DRIVE_FOLDER_ID, progress_callback, cancel_flag)
    elif os.path.isdir(target_path):
        upload_directory(service, target_path, TARGET_DRIVE_FOLDER_ID, progress_callback, cancel_flag)


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

    # Standardize the base directory for precise ledger mapping
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
    
    
    