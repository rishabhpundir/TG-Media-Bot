Google Drive Utilities: Server Cloner & Resumable Downloader
====================================================================

This toolkit contains two Python scripts designed to efficiently manage, clone, and download massive Google Drive folders using the official Google Drive API. 

Environment: Built and tested using Python 3.12.8.

====================================================================
PREREQUISITES
====================================================================
1. Python 3.12.8 installed on your system.
2. A "credentials.json" file generated from Google Cloud Console (with the Google Drive API enabled) placed in the root directory of this project.

====================================================================
INITIAL SETUP
====================================================================
Before running the scripts, set up your isolated virtual environment and install the dependencies.

Open your terminal in the project directory and run:

For Mac/Linux:
--------------
python3.12 -m venv venv
source venv/bin/activate
pip install -r drive_requirements.txt

For Windows:
------------
python -m venv venv
.\venv\Scripts\activate
pip install -r drive_requirements.txt

====================================================================
1. SERVER-SIDE CLONER (drive_cloner.py)
====================================================================
This script clones a public Google Drive folder directly to your personal Google Drive account. The data transfers server-to-server, completely bypassing your local network for instantaneous copying.

To run:
-------
python drive_cloner.py

(Note: Ensure you update the "public_folder_id" variable inside the script with your target Drive link before running).

====================================================================
2. RESUMABLE DOWNLOADER (drive_downloader.py)
====================================================================
A robust, error-tolerant script to download massive datasets directly to your local storage. 
* State Tracking: Maintains a "download_state.json" ledger. If interrupted, it checks local byte sizes against the server and resumes exactly where it left off.
* Auto-Recovery: Built-in incremental backoff automatically restarts downloads if the connection times out.

To run:
-------
python drive_downloader.py

====================================================================
IMPORTANT NOTE ON FIRST RUN
====================================================================
The first time you execute either of these scripts, a web browser will open asking you to authorize the application with your Google account. 

Once authorized, a "token.json" file will be generated in your folder. All future executions of the scripts will use this token silently in the background without needing you to log in again.
