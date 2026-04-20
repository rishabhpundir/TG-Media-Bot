import os
import sys

from tqdm import tqdm
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# Ensure it loads .env from the script's folder, not the current terminal folder
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

SCOPES = ['https://www.googleapis.com/auth/drive.file']

TARGET_DRIVE_FOLDER_ID = os.getenv('TARGET_DRIVE_FOLDER_ID')
LIST_FILE_NAME = 'upload.txt'

# Lock credentials to the script's directory
TOKEN_PATH = os.path.join(SCRIPT_DIR, 'token.json')
CREDS_PATH = os.path.join(SCRIPT_DIR, 'credentials.json')

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


def create_drive_folder(service, folder_name, parent_id):
    """Creates a folder in Google Drive and returns its ID."""
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')


def upload_file(service, file_path, parent_id):
    """Uploads a single file to a specific Google Drive folder with live progress."""
    file_name = os.path.basename(file_path)
    file_metadata = {'name': file_name, 'parents': [parent_id]}
    file_size = os.path.getsize(file_path)
    
    # chunksize must be a multiple of 256KB. 10MB chunk used here for large files.
    chunk_size = 10 * 1024 * 1024 
    media = MediaFileUpload(file_path, chunksize=chunk_size, resumable=True)
    
    request = service.files().create(body=file_metadata, media_body=media, fields='id')
    
    response = None
    
    # Initialize tqdm progress bar (automatically handles speed, percentage, and size parsing)
    with tqdm(total=file_size, unit='B', unit_scale=True, unit_divisor=1024, desc=f"Uploading: {file_name}") as pbar:
        while response is None:
            status, response = request.next_chunk()
            if status:
                # Update the progress bar to match the exact byte count uploaded so far
                pbar.update(status.resumable_progress - pbar.n)
                
    return response.get('id')


def upload_directory(service, dir_path, parent_id):
    """Recursively uploads a directory and its contents to Google Drive."""
    dir_name = os.path.basename(dir_path)
    print(f"Creating Drive folder: {dir_name}...")
    
    # Create the folder in Drive first
    drive_folder_id = create_drive_folder(service, dir_name, parent_id)
    
    # Iterate through the local folder contents
    for item in os.listdir(dir_path):
        item_path = os.path.join(dir_path, item)
        if os.path.isfile(item_path):
            upload_file(service, item_path, drive_folder_id)
        elif os.path.isdir(item_path):
            upload_directory(service, item_path, drive_folder_id)


def main():
    # 1. Validate command line arguments
    if len(sys.argv) < 2:
        print("Usage: python script.py <base_directory>")
        sys.exit(1)

    base_dir = sys.argv[1]
    
    # Strip trailing slash if the user provided one (e.g., /mnt/media/)
    if base_dir.endswith('/'):
        base_dir = base_dir[:-1]
        
    list_file_path = os.path.join(base_dir, LIST_FILE_NAME)

    if not os.path.exists(list_file_path):
        print(f"Error: Could not find '{LIST_FILE_NAME}' in {base_dir}")
        sys.exit(1)

    # 2. Authenticate
    print("Authenticating with Google Drive...")
    service = authenticate()

    # 3. Read the target list
    with open(list_file_path, 'r') as f:
        # Read lines, strip whitespace/newlines, and ignore empty lines
        items = [line.strip() for line in f.readlines() if line.strip()]

    # 4. Process each item
    for item in items:
        full_path = os.path.join(base_dir, item)
        
        if os.path.isfile(full_path):
            upload_file(service, full_path, TARGET_DRIVE_FOLDER_ID)
        elif os.path.isdir(full_path):
            upload_directory(service, full_path, TARGET_DRIVE_FOLDER_ID)
        else:
            print(f"Warning: '{full_path}' does not exist on disk. Skipping.")
            
    print("\nUpload process complete!")


if __name__ == '__main__':
    main()
    
    
    