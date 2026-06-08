"""
poller.py — run by GitHub Actions every hour.
Reads handles.json, finds new videos, downloads + uploads to Drive,
writes videos.json for the Pages dashboard.
"""

import os, json, time, logging, tempfile
from pathlib import Path
from datetime import datetime, timezone

import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poller")

GDRIVE_FOLDER_ID     = os.environ["GDRIVE_FOLDER_ID"]
SERVICE_ACCOUNT_FILE = "service_account.json"
HANDLES_FILE         = Path("handles.json")
VIDEOS_FILE          = Path("videos.json")
SEEN_FILE            = Path("seen_ids.json")
FIRST_RUN_FILE       = Path("first_run_done.json")   # created after first poll
FORCE_HANDLE         = os.environ.get("FORCE_HANDLE", "").strip().lstrip("@")
MAX_RETRIES          = 3    # retry download this many times on transient errors
RETRY_DELAY          = 15   # seconds between retries

# ── helpers ──────────────────────────────────────────────────────────────────

def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default

def save_json(p: Path, data):
    p.write_text(json.dumps(data, indent=2))

# ── Google Drive ──────────────────────────────────────────────────────────────

def gdrive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def upload_to_gdrive(file_path: Path, filename: str):
    svc = gdrive_service()
    meta = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    uploaded = svc.files().create(body=meta, media_body=media, fields="id").execute()
    fid = uploaded["id"]
    svc.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    stream_url = f"https://drive.google.com/uc?export=download&id={fid}"
    embed_url  = f"https://drive.google.com/file/d/{fid}/preview"
    log.info(f"  Uploaded → {fid}")
    return stream_url, embed_url, fid

# ── Instagram download ────────────────────────────────────────────────────────

def get_entries(handle: str) -> list:
    """Fetch flat list of recent reel IDs for a handle. Returns [] on failure."""
    url = f"https://www.instagram.com/{handle}/reels/"
    flat_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True, "playlistend": 30,
        # "cookiefile": "cookies.txt",  # uncomment for private accounts
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            return info.get("entries", [])
        except Exception as e:
            log.warning(f"[@{handle}] flat extract attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)   # back off longer each time
    log.error(f"[@{handle}] All fetch attempts failed — skipping this handle.")
    return []


def download_video(entry: dict, tmpdir: str):
    """Download a single video. Returns (full_info, file_path) or raises."""
    vid_id   = entry["id"]
    page_url = entry.get("url") or entry.get("webpage_url", "")
    out_tmpl = str(Path(tmpdir) / f"{vid_id}.%(ext)s")
    dl_opts  = {
        "quiet": True, "no_warnings": True,
        "format": "mp4/best[ext=mp4]/best",
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "retries": 5,               # yt-dlp internal retries for network blips
        "fragment_retries": 5,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                full = ydl.extract_info(page_url, download=True)
            files = list(Path(tmpdir).glob(f"{vid_id}.*"))
            if not files:
                raise FileNotFoundError(f"No file found for {vid_id} after download")
            return full, files[0]
        except Exception as e:
            log.warning(f"  Download attempt {attempt}/{MAX_RETRIES} failed: {e}")
            # Clean up any partial file
            for f in Path(tmpdir).glob(f"{vid_id}.*"):
                f.unlink(missing_ok=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    raise RuntimeError(f"Download failed after {MAX_RETRIES} attempts")


def fetch_new_for_handle(handle: str, seen: set, tmpdir: str,
                         first_run: bool) -> list[dict]:
    results  = []
    entries  = get_entries(handle)
    new_ids  = [e for e in entries if e.get("id") and e["id"] not in seen]

    if first_run:
        # Mark everything currently on their profile as seen — don't download.
        for e in entries:
            if e.get("id"):
                seen.add(e["id"])
        log.info(f"[@{handle}] First-run: marked {len(entries)} existing videos as seen. "
                 f"Only future videos will be downloaded.")
        return []

    log.info(f"[@{handle}] {len(new_ids)} new video(s) to download.")

    for entry in new_ids:
        vid_id = entry["id"]

        # ── download ──────────────────────────────────────────────────
        try:
            full, file_path = download_video(entry, tmpdir)
        except Exception as e:
            log.error(f"  Skipping {vid_id}: {e}")
            seen.add(vid_id)    # don't retry this video next hour
            continue

        # ── upload ────────────────────────────────────────────────────
        filename = f"{handle}_{vid_id}.mp4"
        try:
            stream_url, embed_url, gdrive_id = upload_to_gdrive(file_path, filename)
        except Exception as e:
            log.error(f"  Drive upload failed for {vid_id}: {e}")
            # Don't mark as seen — we'll retry next hour
            file_path.unlink(missing_ok=True)
            continue
        finally:
            file_path.unlink(missing_ok=True)   # always clean up local copy

        results.append({
            "id":        vid_id,
            "handle":    handle,
            "title":     (full or {}).get("title", "Untitled"),
            "thumbnail": (full or {}).get("thumbnail", ""),
            "timestamp": (full or {}).get("timestamp", int(time.time())),
            "stream_url":  stream_url,
            "embed_url":   embed_url,
            "gdrive_id":   gdrive_id,
            "watched":     False,
            "added_at":    datetime.now(timezone.utc).isoformat(),
            "error":       None,
        })
        seen.add(vid_id)
        log.info(f"  ✓ {vid_id} done.")

    return results

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    handles: list = load_json(HANDLES_FILE, [])
    videos:  list = load_json(VIDEOS_FILE,  [])
    seen:    set  = set(load_json(SEEN_FILE, []))

    # First-run guard: if we've never polled before, just mark everything seen.
    first_run = not FIRST_RUN_FILE.exists()
    if first_run:
        log.info("=== FIRST RUN — existing videos will be skipped, not downloaded ===")

    if FORCE_HANDLE:
        targets = [FORCE_HANDLE]
        log.info(f"Forced single handle: @{FORCE_HANDLE}")
    else:
        targets = handles
        log.info(f"Polling {len(targets)} handle(s): {targets}")

    if not targets:
        log.warning("No handles configured. Add one via the dashboard Settings tab.")
        save_json(VIDEOS_FILE, videos)
        save_json(SEEN_FILE, list(seen))
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        for handle in targets:
            log.info(f"── Checking @{handle} ──")
            new = fetch_new_for_handle(handle, seen, tmpdir, first_run)
            videos.extend(new)
            log.info(f"  {len(new)} new video(s) added.")

    # Mark first run as done so next run downloads normally
    if first_run:
        FIRST_RUN_FILE.write_text("done")
        log.info("First-run marker saved. Future runs will download new videos.")

    save_json(VIDEOS_FILE, videos)
    save_json(SEEN_FILE, list(seen))
    log.info("Done.")

if __name__ == "__main__":
    main()
