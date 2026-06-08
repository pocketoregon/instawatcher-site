"""
delete_video.py — called by the delete.yml workflow.
Deletes a file from Google Drive and marks it watched in videos.json.
"""

import os, json, logging
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("delete")

VIDEO_ID       = os.environ["VIDEO_ID"]
GDRIVE_FILE_ID = os.environ["GDRIVE_FILE_ID"]
VIDEOS_FILE    = Path("videos.json")

def gdrive_service():
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def main():
    # Delete from Drive
    try:
        svc = gdrive_service()
        svc.files().delete(fileId=GDRIVE_FILE_ID).execute()
        log.info(f"Deleted Drive file: {GDRIVE_FILE_ID}")
    except Exception as e:
        log.error(f"Drive delete failed (maybe already gone): {e}")

    # Mark watched in videos.json
    videos = json.loads(VIDEOS_FILE.read_text()) if VIDEOS_FILE.exists() else []
    for v in videos:
        if v["id"] == VIDEO_ID:
            v["watched"] = True
            log.info(f"Marked {VIDEO_ID} as watched")
            break

    VIDEOS_FILE.write_text(json.dumps(videos, indent=2))

if __name__ == "__main__":
    main()
