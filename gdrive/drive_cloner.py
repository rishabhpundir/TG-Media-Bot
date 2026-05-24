import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/drive']

def authenticate():
    creds = None
    # The file token.json stores the user's access and refresh tokens
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

def copy_folder(service, source_folder_id, dest_parent_id):
    # 1. Get the name of the source folder
    source_folder = service.files().get(fileId=source_folder_id, fields='name').execute()
    folder_name = source_folder.get('name')
    print(f"Creating directory: {folder_name}")

    # 2. Create the exact same folder in your Drive
    folder_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [dest_parent_id]
    }
    new_folder = service.files().create(body=folder_metadata, fields='id').execute()
    new_folder_id = new_folder.get('id')

    # 3. List all items inside the public source folder
    query = f"'{source_folder_id}' in parents and trashed=false"
    page_token = None

    while True:
        results = service.files().list(
            q=query,
            spaces='drive',
            fields='nextPageToken, files(id, name, mimeType)',
            pageToken=page_token
        ).execute()

        items = results.get('files', [])

        for item in items:
            # If it's a folder, run the function recursively
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                copy_folder(service, item['id'], new_folder_id)
            else:
                # If it's a file, trigger the server-side clone
                print(f"  -> Cloning file: {item['name']}")
                file_metadata = {
                    'name': item['name'],
                    'parents': [new_folder_id]
                }
                service.files().copy(
                    fileId=item['id'],
                    body=file_metadata
                ).execute()

        page_token = results.get('nextPageToken', None)
        if page_token is None:
            break

def main():
    service = authenticate()
    
    # The ID from the URL you shared earlier
    public_folder_id = '1HGSUjQVvzgEXDEPaLtcbhp4dZ5rf8nVn'
    
    # Where you want it saved. 'root' places it in your main Drive directory.
    # If you want it in a specific folder, replace 'root' with that folder's ID.
    my_drive_parent_id = 'root' 
    
    print("Initiating server-side copy...")
    copy_folder(service, public_folder_id, my_drive_parent_id)
    print("Copy completed successfully!")

if __name__ == '__main__':
    main()
    
    