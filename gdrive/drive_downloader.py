import os
import io
import time
import json
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive']
STATE_FILE = 'download_state.json'

def authenticate():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

def format_size(bytes_size):
    """Converts bytes to a human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0

def scan_drive_folder(service, folder_id, current_path=""):
    """Recursively scans the Drive folder to map all files and their sizes."""
    files_list = []
    query = f"'{folder_id}' in parents and trashed=false"
    page_token = None
    
    while True:
        results = service.files().list(
            q=query,
            spaces='drive',
            fields='nextPageToken, files(id, name, mimeType, size)',
            pageToken=page_token
        ).execute()
        
        items = results.get('files', [])
        
        for item in items:
            # Use forward slashes for cross-platform JSON consistency
            item_path = f"{current_path}/{item['name']}".strip("/")
            
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                files_list.extend(scan_drive_folder(service, item['id'], item_path))
            elif not item['mimeType'].startswith('application/vnd.google-apps'):
                item['relative_path'] = item_path
                files_list.append(item)
                
        page_token = results.get('nextPageToken', None)
        if not page_token:
            break
            
    return files_list

def load_state():
    """Loads the JSON state file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_state(state_data):
    """Saves the JSON state file."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state_data, f, indent=4)

def download_file_with_retry(service, file_id, file_path, file_name, file_size, relative_path, state_data):
    """Downloads a file with exponential/incremental backoff on failure."""
    base_wait = 30
    increment = 15
    attempt = 0
    
    # Ensure local directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    while True:
        try:
            print(f"Downloading: {file_name} ({format_size(file_size)})")
            request = service.files().get_media(fileId=file_id)
            
            with io.FileIO(file_path, mode='wb') as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=50*1024*1024)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        # Print progress on the same line to reduce console flooding
                        print(f"  -> {int(status.progress() * 100)}% complete", end='\r')
            
            print(f"\nFinished: {file_name}\n")
            
            # File downloaded successfully. Update the JSON state.
            state_data[relative_path] = {
                "name": file_name,
                "size": file_size
            }
            save_state(state_data)
            break # Break out of the retry loop
            
        except Exception as e:
            wait_time = base_wait + (attempt * increment)
            print(f"\n[!] Error encountered: {e}")
            print(f"[!] Read operation failed. Retrying in {wait_time} seconds...\n")
            time.sleep(wait_time)
            attempt += 1

def main():
    service = authenticate()
    
    public_folder_id = '1HGSUjQVvzgEXDEPaLtcbhp4dZ5rf8nVn'
    
    # Get root folder name
    folder_meta = service.files().get(fileId=public_folder_id, fields='name').execute()
    root_folder_name = folder_meta.get('name')
    local_download_path = os.path.join(os.getcwd(), root_folder_name)
    
    print("Scanning Google Drive folder layout. Please wait...")
    all_files = scan_drive_folder(service, public_folder_id)
    
    total_bytes = sum(int(f.get('size', 0)) for f in all_files)
    print(f"Total folder size to process: {format_size(total_bytes)}")
    print("-" * 50)
    
    state_data = load_state()
    skipped_count = 0
    files_to_download = []
    
    # Filter files that are already completed
    for item in all_files:
        rel_path = item['relative_path']
        item_size = int(item.get('size', 0))
        local_file_path = os.path.join(local_download_path, os.path.normpath(rel_path))
        
        # 1. Check if the file is perfectly downloaded on the local disk
        if os.path.exists(local_file_path) and os.path.getsize(local_file_path) == item_size:
            # 2. If it's on disk but missing from our JSON ledger, heal the ledger
            if rel_path not in state_data:
                state_data[rel_path] = {
                    "name": item['name'],
                    "size": item_size
                }
                # Save the healed state back to the file
                save_state(state_data)
                
            skipped_count += 1
            continue
                
        # If it doesn't exist, or the size is wrong (partial download), queue it up
        files_to_download.append((item, local_file_path))
    
    if skipped_count > 0:
        print(f"Skipped {skipped_count} previously downloaded files.")
        print("-" * 50)
        
    if not files_to_download:
        print("All files are fully downloaded! Nothing left to do.")
        return

    # Process remaining files
    for item, local_file_path in files_to_download:
        item_id = item['id']
        item_name = item['name']
        item_size = int(item.get('size', 0))
        rel_path = item['relative_path']
        
        download_file_with_retry(service, item_id, local_file_path, item_name, item_size, rel_path, state_data)

    print("Download completed successfully!")

if __name__ == '__main__':
    main()
    
    