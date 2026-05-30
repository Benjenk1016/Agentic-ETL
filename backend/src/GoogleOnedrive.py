# backend/src/GoogleOnedrive.py
#
# Folder-based Google Drive sync (triggered by your chat command / API call)
# - Scans a Google Drive folder for new/updated files
# - Exports Google Docs Editors files (Sheets->.xlsx, Docs->.docx, Slides->.pptx, Drawings->.png)
# - Downloads binary files as-is (xlsx, pdf, png, zip, etc.)
# - Skips folders (optionally you can add recursion later)
# - Writes to BACKUP_DIR (Docker-safe: mount your OneDrive folder to /data/GoogleSheetsBackups)
# - Persists state in STATE_FILE so repeated runs only pull changes
#
# Endpoint: POST /run/google_to_onedrive
#
# Required env:
#   GOOGLE_SERVICE_ACCOUNT_FILE=/app/backend/src/google-service-account-key.json   (or a mounted secret path)
#   GOOGLE_DRIVE_FOLDER_ID=<Drive folder id to scan>
#
# Recommended env:
#   BACKUP_DIR=/data/GoogleSheetsBackups
#   STATE_FILE=/data/onedrive/google_seen.json
#
# Optional OneDrive Graph upload (only if you really need upload via Graph on top of local OneDrive sync):
#   UPLOAD_TO_ONEDRIVE=true
#   MS_CLIENT_ID=...
#   MS_CLIENT_SECRET=...
#   MS_TENANT_ID=...
#   MS_USER_EMAIL=...

import os
import io
import re
import time
import json
import shutil
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from msal import ConfidentialClientApplication

PROJECT_INPUT_DIR = Path("/data/input")

# ----------------------------
# FastAPI App
# ----------------------------
app = FastAPI(title="Agentic ETL API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ----------------------------
# Configuration (Docker-safe)
# ----------------------------
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def log(msg: str) -> None:
    print(f"[GoogleOnedrive] {msg}", flush=True)


# Local backup directory INSIDE the container.
# In docker-compose, mount your Mac OneDrive folder to /data/GoogleSheetsBackups
BACKUP_DIR = Path(_env("BACKUP_DIR", "/data/GoogleSheetsBackups")).resolve()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# State/manifest so we only download changes
_default_state_dir = _env("STATE_DIR", "./data/state")
STATE_FILE = Path(_env("STATE_FILE", str(Path(_default_state_dir) / "google_seen.json"))).resolve()

# Google
GOOGLE_SERVICE_ACCOUNT_FILE = _env("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
GOOGLE_DRIVE_FOLDER_ID = _env("GOOGLE_DRIVE_FOLDER_ID")

# Microsoft / Graph (optional upload)
MS_CLIENT_ID = _env("MS_CLIENT_ID")
MS_CLIENT_SECRET = _env("MS_CLIENT_SECRET")
MS_TENANT_ID = _env("MS_TENANT_ID")
MS_USER_EMAIL = _env("MS_USER_EMAIL", "user@example.com")

UPLOAD_TO_ONEDRIVE = _env("UPLOAD_TO_ONEDRIVE", "false").lower() in ("1", "true", "yes", "y")
ONEDRIVE_REMOTE_FOLDER = _env("ONEDRIVE_REMOTE_FOLDER", "GoogleSheetsBackups")


# ----------------------------
# OneDrive Manager (Graph) - optional
# ----------------------------
class OneDriveManager:
    """Handles OneDrive operations via Microsoft Graph API (application permissions)."""

    def __init__(self, client_id: str, client_secret: str, tenant_id: str, user_email: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.user_email = user_email

        self.token: Optional[str] = None
        self.base_url = "https://graph.microsoft.com/v1.0"
        self.user_id: Optional[str] = None
        self.drive_id: Optional[str] = None

    def authenticate(self) -> bool:
        app = ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
        )

        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            log(f"Graph auth failed: {result.get('error_description')}")
            return False

        self.token = result["access_token"]
        return self._get_user_and_drive()

    def _get_user_and_drive(self) -> bool:
        headers = self.get_headers()

        user_url = f"{self.base_url}/users/{self.user_email}"
        r = requests.get(user_url, headers=headers)
        if r.status_code != 200:
            log(f"Failed to find user {self.user_email}: {r.text}")
            return False

        user = r.json()
        self.user_id = user.get("id")

        drive_url = f"{self.base_url}/users/{self.user_id}/drive"
        r = requests.get(drive_url, headers=headers)
        if r.status_code != 200:
            log(f"Failed to get drive for user {self.user_email}: {r.text}")
            return False

        self.drive_id = r.json().get("id")
        log(f"Graph ready. user={self.user_email}, drive_id={self.drive_id}")
        return True

    def get_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def ensure_folder(self, remote_folder: str) -> Tuple[bool, str]:
        if not self.drive_id:
            return False, "Drive not initialized"

        parts = [p for p in remote_folder.split("/") if p.strip()]
        parent_id = "root"

        for part in parts:
            url = f"{self.base_url}/drives/{self.drive_id}/items/{parent_id}/children"
            headers = self.get_headers()
            r = requests.get(url, headers=headers)
            if r.status_code != 200:
                return False, f"List children failed: {r.status_code} {r.text}"

            found_id = None
            for item in r.json().get("value", []):
                if item.get("name") == part and "folder" in item:
                    found_id = item.get("id")
                    break

            if found_id:
                parent_id = found_id
                continue

            create_url = f"{self.base_url}/drives/{self.drive_id}/items/{parent_id}/children"
            payload = {"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"}
            r = requests.post(create_url, headers=headers, json=payload)
            if r.status_code not in (200, 201):
                return False, f"Create folder '{part}' failed: {r.status_code} {r.text}"

            parent_id = r.json().get("id")

        return True, parent_id

    def upload_file_bytes(self, remote_folder: str, filename: str, content: bytes) -> Tuple[bool, str]:
        if not self.drive_id:
            return False, "Drive not initialized"

        ok, folder_item_id_or_msg = self.ensure_folder(remote_folder)
        if not ok:
            return False, folder_item_id_or_msg

        folder_item_id = folder_item_id_or_msg
        upload_url = f"{self.base_url}/drives/{self.drive_id}/items/{folder_item_id}:/{filename}:/content"
        headers = {"Authorization": f"Bearer {self.token}"}

        r = requests.put(upload_url, headers=headers, data=content)
        if r.status_code not in (200, 201):
            return False, f"Upload failed: {r.status_code} {r.text}"

        return True, "Uploaded to OneDrive successfully"


# ----------------------------
# Google Drive helpers
# ----------------------------
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

EXPORT_MAP: Dict[str, Tuple[str, str]] = {
    # Google Sheets -> XLSX
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    # Google Docs -> DOCX
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    # Google Slides -> PPTX
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    # Google Drawings -> PNG (optional)
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def get_drive_service():
    if not GOOGLE_SERVICE_ACCOUNT_FILE or not Path(GOOGLE_SERVICE_ACCOUNT_FILE).exists():
        raise RuntimeError(
            f"GOOGLE_SERVICE_ACCOUNT_FILE not found at '{GOOGLE_SERVICE_ACCOUNT_FILE}'. "
            f"Set env GOOGLE_SERVICE_ACCOUNT_FILE to a valid path inside the container."
        )

    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=GOOGLE_SCOPES,
    )
    return build("drive", "v3", credentials=creds)


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "", name).strip()
    return name or f"file_{int(time.time())}"


def export_google_editor_file_bytes(drive_service, file_id: str, export_mime: str) -> bytes:
    request = drive_service.files().export_media(fileId=file_id, mimeType=export_mime)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


def download_drive_file_bytes(drive_service, file_id: str) -> bytes:
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()



def save_local_file(filename: str, content: bytes) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = (BACKUP_DIR / filename).resolve()
    with open(backup_path, "wb") as f:
        f.write(content)

    project_input_path = (PROJECT_INPUT_DIR / filename).resolve()
    with open(project_input_path, "wb") as f:
        f.write(content)

    print(f"Saved to backup: {backup_path}")
    print(f"Saved to input: {project_input_path}")
    return backup_path
    
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def list_folder_files(drive_service, folder_id: str) -> List[Dict[str, Any]]:
    q = f"'{folder_id}' in parents and trashed=false"
    fields = "nextPageToken, files(id,name,mimeType,modifiedTime,size)"
    files: List[Dict[str, Any]] = []
    token = None

    while True:
        resp = drive_service.files().list(
            q=q,
            fields=fields,
            pageToken=token,
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break

    return files

def list_folder_tree_files(drive_service, root_folder_id: str) -> List[Dict[str, Any]]:
    """
    Recursively list ALL files under a folder (including subfolders).
    Returns only non-folder items in the final list.
    """
    all_files: List[Dict[str, Any]] = []
    queue: List[str] = [root_folder_id]

    while queue:
        folder_id = queue.pop(0)
        items = list_folder_files(drive_service, folder_id)

        for it in items:
            if it.get("mimeType") == GOOGLE_FOLDER_MIME:
                queue.append(it["id"])
            else:
                all_files.append(it)

    return all_files

# ----------------------------
# Main sync (triggered by chat command / API)
# ----------------------------
def run_download_process() -> Tuple[bool, str]:
    """
    Scans GOOGLE_DRIVE_FOLDER_ID and downloads ONLY new/updated items into BACKUP_DIR.
    - Exports Google-native Docs/Sheets/Slides (EXPORT_MAP)
    - Downloads binary files as-is
    - Skips folders
    """
    try:
        if not GOOGLE_DRIVE_FOLDER_ID:
            return False, "Missing GOOGLE_DRIVE_FOLDER_ID env var (set it to the Drive folder ID)."

        log(f"Scanning folder: {GOOGLE_DRIVE_FOLDER_ID}")
        log(f"BACKUP_DIR: {BACKUP_DIR}")
        log(f"STATE_FILE: {STATE_FILE}")

        drive = get_drive_service()
        state = load_state()

        files = list_folder_tree_files(drive, GOOGLE_DRIVE_FOLDER_ID)
        if not files:
            # Materialize state file/folder on first run even when folder is empty.
            save_state(state)
            return True, "No files found in the Google Drive folder."

        # Optional OneDrive upload setup
        odm: Optional[OneDriveManager] = None
        if UPLOAD_TO_ONEDRIVE:
            missing = [k for k in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID") if not _env(k)]
            if missing:
                return False, f"UPLOAD_TO_ONEDRIVE=true but missing env vars: {', '.join(missing)}"

            odm = OneDriveManager(MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, MS_USER_EMAIL)
            if not odm.authenticate():
                return False, "Microsoft Graph authentication failed."

        downloaded = 0
        skipped = 0
        uploaded = 0
        skipped_folders = 0

        for f in files:
            file_id = f["id"]
            name = f.get("name", "Unnamed")
            mime = f.get("mimeType", "")
            modified = f.get("modifiedTime")

            if mime == GOOGLE_FOLDER_MIME:
                skipped_folders += 1
                continue

            prev = state["files"].get(file_id)
            if prev and prev.get("modifiedTime") == modified:
                skipped += 1
                continue

            safe_name = sanitize_filename(name)

            # Export Google-native editor files; download binary files
            if mime in EXPORT_MAP:
                export_mime, ext = EXPORT_MAP[mime]
                filename = f"{safe_name}{ext}"
                content = export_google_editor_file_bytes(drive, file_id, export_mime)
            else:
                filename = safe_name
                content = download_drive_file_bytes(drive, file_id)

            local_path = save_local_file(filename, content)
            downloaded += 1
            log(f"Saved: {local_path}")

            if odm:
                ok, msg = odm.upload_file_bytes(ONEDRIVE_REMOTE_FOLDER, filename, content)
                if not ok:
                    return False, msg
                uploaded += 1
                log(f"OneDrive upload: {msg}")

            state["files"][file_id] = {
                "name": name,
                "mimeType": mime,
                "modifiedTime": modified,
            }

        save_state(state)

        return True, (
            f"Sync complete. Downloaded/updated: {downloaded}, "
            f"skipped unchanged: {skipped}, skipped folders: {skipped_folders}, "
            f"uploaded: {uploaded}."
        )

    except HttpError as e:
        return False, f"Google API error: {e}"
    except Exception as e:
        return False, f"Error: {e}"


@app.post("/run/google_to_onedrive")
def run_google_to_onedrive():
    success, message = run_download_process()
    return {"ok": success, "message": message}
