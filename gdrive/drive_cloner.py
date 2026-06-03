import os

from gdrive.auth import get_service

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/drive']

def authenticate():
    return get_service(SCOPES)

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
    
    