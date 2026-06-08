"""
manage_handles.py — called by the handles.yml workflow.
Adds or removes a handle from handles.json.
"""

import os, json, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("handles")

ACTION       = os.environ["ACTION"].strip().lower()   # "add" or "remove"
HANDLE       = os.environ["HANDLE"].strip().lstrip("@").lower()
HANDLES_FILE = Path("handles.json")

def main():
    handles: list = json.loads(HANDLES_FILE.read_text()) if HANDLES_FILE.exists() else []

    if ACTION == "add":
        if HANDLE not in handles:
            handles.append(HANDLE)
            log.info(f"Added @{HANDLE}")
        else:
            log.info(f"@{HANDLE} already present")

    elif ACTION == "remove":
        before = len(handles)
        handles = [h for h in handles if h != HANDLE]
        log.info(f"Removed @{HANDLE}" if len(handles) < before else f"@{HANDLE} not found")

    else:
        raise ValueError(f"Unknown action: {ACTION}")

    HANDLES_FILE.write_text(json.dumps(handles, indent=2))
    log.info(f"handles.json → {handles}")

if __name__ == "__main__":
    main()
