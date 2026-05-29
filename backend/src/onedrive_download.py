import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
import msal
import pandas as pd
import requests

from backend.src.etl_utils import default_input_dir, default_state_dir, ensure_data_folder, status, warn, error

# Load environment variables from .env file
load_dotenv()

# TODO: Business Account Migration
# When this is moved to a business OneDrive account:
# 1. Create an app with account type "My organization only"
# 2. Update ONEDRIVE_TENANT in .env to your tenant ID (e.g., "tenant.onmicrosoft.com")
# 3. Adjust API permissions if needed for your organization's policies
# 4. The device code flow will still work; users authenticate with their org account

EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}
MANIFEST_COLUMNS = [
    "item_id",
    "name",
    "remote_path",
    "local_path",
    "etag",
    "last_modified",
    "size",
    "sha256",
    "downloaded_at",
    "last_checked_at",
]


@dataclass
class SyncConfig:
    client_id: str
    tenant: str
    remote_folder: str
    download_dir: Path
    manifest_path: Path
    token_cache_path: Path
    flatten: bool
    recursive: bool
    dry_run: bool
    interactive: bool


# Replaces characters not allowed in Windows filenames.
def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:\\"/|?*]', "_", name)


# Returns the current UTC time as an ISO 8601 string with Z suffix.
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Loads the manifest Excel sheet into a DataFrame, ensuring expected columns exist.
def load_manifest(path: Path) -> pd.DataFrame:
    if path.exists():
        df = pd.read_excel(path)
    else:
        df = pd.DataFrame(columns=MANIFEST_COLUMNS)

    for col in MANIFEST_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df


# Writes the manifest DataFrame to disk, creating directories if needed.
def save_manifest(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(path, index=False)


# Updates or inserts a manifest row keyed by item_id.
def upsert_manifest_row(df: pd.DataFrame, row: dict) -> pd.DataFrame:
    if df.empty or "item_id" not in df.columns:
        return pd.DataFrame([row], columns=MANIFEST_COLUMNS)

    mask = df["item_id"] == row["item_id"]
    if mask.any():
        for key, value in row.items():
            df.loc[mask, key] = value
        return df

    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


# Reads a token cache from disk if available.
def load_token_cache(cache_path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            warn(f"Token cache is invalid or unreadable, continuing with a fresh cache: {exc}")
    return cache


# Saves the token cache if it has changed.
def save_token_cache(cache: msal.SerializableTokenCache, cache_path: Path) -> None:
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")


# Acquires an access token using device code flow for personal accounts.
def acquire_access_token(config: SyncConfig) -> str:
    cache = load_token_cache(config.token_cache_path)
    app = msal.PublicClientApplication(
        client_id=config.client_id,
        authority=f"https://login.microsoftonline.com/{config.tenant}",
        token_cache=cache,
    )

    scopes = ["Files.Read.All"]
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(scopes=scopes, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=scopes)
        if "message" not in flow:
            error(f"Device flow initiation failed. Response: {flow}")
            raise RuntimeError(f"Failed to initiate device code flow: {flow}")
        status(flow["message"])
        result = app.acquire_token_by_device_flow(flow, timeout=30)

    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result}")

    save_token_cache(cache, config.token_cache_path)
    return result["access_token"]


# Builds a Microsoft Graph session with an access token.
def build_graph_session(access_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})
    return session


def resolve_authenticated_user(session: requests.Session) -> str:
    try:
        response = session.get(
            "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName,mail",
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return (
            payload.get("userPrincipalName")
            or payload.get("mail")
            or payload.get("displayName")
            or "unknown"
        )
    except requests.exceptions.RequestException:
        return "unknown"
    except ValueError:
        return "unknown"


# Resolves the starting folder URL for listing children.
def resolve_folder_list_url(config: SyncConfig) -> str:
    remote_folder = config.remote_folder.strip()
    if remote_folder in {"", "/"}:
        return "https://graph.microsoft.com/v1.0/me/drive/root/children"

    remote_folder = remote_folder.lstrip("/").rstrip("/")  # Remove leading and trailing slashes
    return f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_folder}:/children"


# Iterates items in a folder, following @odata.nextLink for pagination.
def iter_children(session: requests.Session, list_url: str) -> Iterable[dict]:
    next_url = list_url
    while next_url:
        try:
            response = session.get(next_url, timeout=60)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                error(f"Folder not found: {next_url}")
                error("The folder path in ONEDRIVE_REMOTE_FOLDER does not exist.")
                error("Check your .env file and make sure the folder exists in OneDrive.")
                error("")
                error("To find the correct path, try:")
                error("  • Set ONEDRIVE_REMOTE_FOLDER=/ to sync from root")
                error("  • Or list your OneDrive folders to find the exact path")
                raise RuntimeError(f"OneDrive folder not found: {list_url}")
            else:
                error(f"API Error {e.response.status_code}: {e.response.text}")
                raise
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to list OneDrive folder items: {e}") from e
        
        try:
            payload = response.json()
        except ValueError as e:
            raise RuntimeError(f"Received invalid JSON from Microsoft Graph: {e}") from e
        for item in payload.get("value", []):
            yield item
        next_url = payload.get("@odata.nextLink")


# Downloads a file and returns its SHA256 hash.
def download_file(url: str, local_path: Path, session: requests.Session | None = None) -> str:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = local_path.with_suffix(local_path.suffix + ".part")
    request_get = session.get if session else requests.get

    sha256 = hashlib.sha256()
    try:
        with request_get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    sha256.update(chunk)
                    handle.write(chunk)
    except (requests.exceptions.RequestException, OSError) as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download file from OneDrive: {exc}") from exc

    temp_path.replace(local_path)
    return sha256.hexdigest()


# Builds a safe local path using the original filename.
def resolve_local_path(
    download_dir: Path,
    name: str,
    item_id: str,
    existing_local: str | None,
) -> Path:
    if existing_local:
        return Path(existing_local)

    safe_name = sanitize_filename(name)
    candidate = download_dir / safe_name
    if not candidate.exists():
        return candidate

    suffix_tag = (item_id or "item")[:8]
    stem = candidate.stem
    ext = candidate.suffix
    candidate = download_dir / f"{stem}__{suffix_tag}{ext}"
    counter = 1
    while candidate.exists():
        candidate = download_dir / f"{stem}__{suffix_tag}_{counter}{ext}"
        counter += 1
    return candidate


def archive_existing_file(local_path: Path, archive_dir: Path) -> Path | None:
    if not local_path.exists():
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = local_path.stem
    suffix = local_path.suffix
    archived_path = archive_dir / f"{stem}__archived_{timestamp}{suffix}"
    counter = 1

    while archived_path.exists():
        archived_path = archive_dir / f"{stem}__archived_{timestamp}_{counter}{suffix}"
        counter += 1

    local_path.replace(archived_path)
    return archived_path


# Returns the remote path string from a Graph item.
def build_remote_path(item: dict) -> str:
    parent_path = item.get("parentReference", {}).get("path", "")
    name = item.get("name", "")

    if "root:" in parent_path:
        parent_path = parent_path.split("root:", 1)[-1]

    if not parent_path.startswith("/"):
        parent_path = f"/{parent_path}" if parent_path else ""

    return f"{parent_path}/{name}".replace("//", "/")


# Determines whether a file should be downloaded based on manifest data.
def needs_download(existing: dict | None, etag: str, last_modified: str, size: int) -> bool:
    if not existing:
        return True
    if etag and existing.get("etag") != etag:
        return True
    if not etag:
        return existing.get("last_modified") != last_modified or existing.get("size") != size
    return False


# Parses CLI arguments and environment variables into a SyncConfig.
def parse_args() -> SyncConfig:
    parser = argparse.ArgumentParser(description="Download changed Excel/CSV files from OneDrive.")
    parser.add_argument("--remote-folder", default=os.environ.get("ONEDRIVE_REMOTE_FOLDER", "/"))
    parser.add_argument("--download-dir", default=os.environ.get("ONEDRIVE_DOWNLOAD_DIR", default_input_dir()))
    parser.add_argument(
        "--manifest",
        default=os.environ.get("ONEDRIVE_MANIFEST", str(Path(default_state_dir()) / "onedrive_manifest.xlsx")),
    )
    parser.add_argument(
        "--token-cache",
        default=os.environ.get("ONEDRIVE_TOKEN_CACHE", str(Path(default_state_dir()) / "token_cache.bin")),
    )
    parser.add_argument("--client-id", default=os.environ.get("ONEDRIVE_CLIENT_ID", ""))
    parser.add_argument("--tenant", default=os.environ.get("ONEDRIVE_TENANT", "consumers"))
    parser.add_argument("--recursive", action="store_true", default=True)
    parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interactive", action="store_true", help="Prompt user (Y/N) for each file before downloading.")
    args = parser.parse_args()

    if not args.client_id:
        raise RuntimeError("ONEDRIVE_CLIENT_ID is required.")

    return SyncConfig(
        client_id=args.client_id,
        tenant=args.tenant,
        remote_folder=args.remote_folder,
        download_dir=Path(args.download_dir),
        manifest_path=Path(args.manifest),
        token_cache_path=Path(args.token_cache),
        flatten=True,
        recursive=args.recursive,
        dry_run=args.dry_run,
        interactive=args.interactive,
    )


# Checks if a valid token exists and authenticates if needed.
# In interactive mode (terminal), automatically authenticates if token is missing.
# In non-interactive mode (Docker), raises error with setup instructions.
def ensure_token_exists(config: SyncConfig) -> None:
    """Verify token cache exists; authenticate if needed."""
    if config.token_cache_path.exists():
        return  # Token already exists, proceed

    # Token is missing
    if sys.stdin.isatty():
        # Interactive mode: user has a terminal, can authenticate now
        status("No OneDrive token found. Authenticating now...")
        try:
            acquire_access_token(config)
            status("✓ Authentication successful! Your token is saved.")
            return
        except RuntimeError as e:
            error(f"Authentication failed: {e}")
            sys.exit(1)
    else:
        # Non-interactive mode (Docker): can't show device code, give clear instructions
        error("OneDrive authentication token not found.")
        error("This is required to sync files from OneDrive.")
        error("")
        error("To set up OneDrive authentication, run this command on your computer:")
        error(f"  python -m backend.src.onedrive_download")
        error("")
        error("This will:")
        error("  1. Prompt you to authenticate with Microsoft")
        error(f"  2. Save your token to {config.token_cache_path}")
        error("  3. The token will be reused on future runs")
        error("")
        sys.exit(1)



# Prompts user to confirm download of a file (Y/N).
def prompt_user_download(name: str, size: int) -> bool:
    size_mb = size / (1024 * 1024)
    try:
        response = input(f"Download '{name}' ({size_mb:.2f} MB)? (y/n): ").strip().lower()
        return response in {"y", "yes"}
    except EOFError:
        # If stdin is not available (e.g., Docker without -i flag), default to yes
        status(f"(stdin not available, auto-confirming download of '{name}')")
        return True


# Main entry point to sync changed Excel files from OneDrive.
def main() -> None:
    config = parse_args()
    ensure_data_folder(str(config.download_dir))
    
    # Check if token exists; authenticate if needed
    ensure_token_exists(config)

    access_token = acquire_access_token(config)
    session = build_graph_session(access_token)
    user_label = resolve_authenticated_user(session)

    manifest = load_manifest(config.manifest_path)
    manifest_indexed = manifest.set_index("item_id", drop=False) if not manifest.empty else manifest

    status("Listing OneDrive items...")
    list_url = resolve_folder_list_url(config)
    archive_dir = config.download_dir / "archive"
    queue = [list_url]
    downloaded = 0
    checked = 0
    new_downloaded = 0
    modified_downloaded = 0
    downloaded_files: list[str] = []

    while queue:
        current_url = queue.pop(0)
        for item in iter_children(session, current_url):
            if "folder" in item and config.recursive:
                folder_id = item.get("id")
                if folder_id:
                    queue.append(f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children")
                continue

            if "file" not in item:
                continue

            name = item.get("name", "")
            suffix = Path(name).suffix.lower()
            if suffix not in EXCEL_EXTENSIONS:
                continue

            checked += 1
            item_id = item.get("id")
            existing = None
            if not manifest_indexed.empty and item_id in manifest_indexed.index:
                existing = manifest_indexed.loc[item_id].to_dict()

            etag = item.get("eTag", "")
            last_modified = item.get("fileSystemInfo", {}).get("lastModifiedDateTime", "")
            size = item.get("size", 0)

            # If this item has no manifest entry yet, check whether a same-named file
            # already exists on disk that is not claimed by any other manifest row.
            # If so it is an untracked orphan — delete it now so resolve_local_path
            # returns the clean base name instead of creating a suffixed duplicate.
            if not existing:
                safe_name = sanitize_filename(name)
                candidate = config.download_dir / safe_name
                if candidate.exists():
                    claimed_paths = set(
                        str(Path(p)) for p in manifest_indexed["local_path"].dropna()
                    ) if not manifest_indexed.empty and "local_path" in manifest_indexed.columns else set()
                    if str(candidate.resolve()) not in {str(Path(p).resolve()) for p in claimed_paths}:
                        candidate.unlink()
                        status(f"Deleted orphan: {candidate.name}")

            local_path = resolve_local_path(
                config.download_dir,
                name,
                item_id,
                existing.get("local_path") if existing else None,
            )

            if not needs_download(existing, etag, last_modified, size):
                row = {
                    "item_id": item_id,
                    "name": name,
                    "remote_path": build_remote_path(item),
                    "local_path": str(local_path),
                    "etag": etag,
                    "last_modified": last_modified,
                    "size": size,
                    "sha256": existing.get("sha256", "") if existing else "",
                    "downloaded_at": existing.get("downloaded_at", "") if existing else "",
                    "last_checked_at": utc_now_iso(),
                }
                manifest = upsert_manifest_row(manifest, row)
                continue

            # If interactive mode and file needs download, ask user
            if config.interactive and not prompt_user_download(name, size):
                status(f"Skipped: {name} (user declined)")
                continue

            download_url = item.get("@microsoft.graph.downloadUrl")
            if not download_url:
                download_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/content"

            is_new = existing is None

            if config.dry_run:
                status(f"Would download: {name}")
                if local_path.exists():
                    status(f"Would archive: {local_path.name}")
                sha256 = existing.get("sha256", "") if existing else ""
            else:
                archived_path = None
                if local_path.exists():
                    archived_path = archive_existing_file(local_path, archive_dir)
                    if archived_path:
                        status(f"Archived: {archived_path.name}")
                status(f"Downloading: {name}")
                try:
                    sha256 = download_file(download_url, local_path, session=session)
                except RuntimeError as exc:
                    error(f"Failed to download '{name}': {exc}")
                    # Preserve prior file if we archived it before a failed download attempt.
                    if archived_path and not local_path.exists() and archived_path.exists():
                        archived_path.replace(local_path)
                        warn(f"Restored previous version of '{name}' after failed download.")
                    continue
                downloaded += 1
                if is_new:
                    new_downloaded += 1
                else:
                    modified_downloaded += 1
                downloaded_files.append(name)

            row = {
                "item_id": item_id,
                "name": name,
                "remote_path": build_remote_path(item),
                "local_path": str(local_path),
                "etag": etag,
                "last_modified": last_modified,
                "size": size,
                "sha256": sha256,
                "downloaded_at": utc_now_iso() if not config.dry_run else existing.get("downloaded_at", ""),
                "last_checked_at": utc_now_iso(),
            }
            manifest = upsert_manifest_row(manifest, row)

    save_manifest(manifest, config.manifest_path)
    status(f"OneDrive user checked: {user_label}")
    status(f"OneDrive folder checked: {config.remote_folder or '/'}")
    status(f"Recursive enabled: {config.recursive}")
    status(f"Checked Excel files: {checked}")
    status(f"Downloaded: {downloaded}")
    status(f"Downloaded new files: {new_downloaded}")
    status(f"Downloaded modified files: {modified_downloaded}")
    if downloaded_files:
        status("Downloaded files: " + " | ".join(downloaded_files))
    else:
        status("Downloaded files: (none)")


if __name__ == "__main__":
    main()
