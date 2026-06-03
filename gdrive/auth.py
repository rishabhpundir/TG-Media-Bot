import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Project root = parent of the gdrive/ package (matches gdriveup.py's anchoring)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEFAULT_TOKEN_PATH = os.path.join(PROJECT_ROOT, 'token.json')
DEFAULT_CREDS_PATH = os.path.join(PROJECT_ROOT, 'credentials.json')


def get_service(scopes, token_path=DEFAULT_TOKEN_PATH, creds_path=DEFAULT_CREDS_PATH):
    """Single OAuth 2.0 entry point for every Drive module.

    `scopes` is a list, e.g. ['https://www.googleapis.com/auth/drive.file'].
    Returns an authenticated googleapiclient 'drive' v3 service.
    """
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())

    return build('drive', 'v3', credentials=creds)


