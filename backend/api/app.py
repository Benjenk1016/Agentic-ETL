import os
import re
import shutil
import subprocess
import sys
import logging
import json
import tempfile
import time
import uuid
import pandas as pd
import msal
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from backend.api.llm_prompt import (
    query_llm,
    query_llm_stream,
    warmup_llm,
    ensure_prompt_directories,
    archive_prompt_record,
    save_combined_response_record,
)
from backend.api.analysis_utils import (
    compare_payload_columns,
    format_payload_for_llm,
)

from backend.src.etl_utils import default_hash_dir, default_output_dir, default_input_dir, default_state_dir, default_archive_dir, default_changed_files_path
from backend.src.onedrive_download import load_token_cache, save_token_cache
from backend.src.file_change_detector import (
    compare_file_without_updating,
    save_current_file_as_new_version,
)
from backend.src.column_extract import extract_columns, extract_data_rows

app = FastAPI(title="Agentic ETL API")
logger = logging.getLogger("uvicorn.error")

ANALYZE_VALID_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}
ANALYZE_DEFAULT_CONFIG_ENV_KEY = "ANALYZE_DEFAULT_CONFIG_FILE"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYZE_CONFIG_DIR = Path("/data/config")
ANALYZE_MAX_WORDS = int(os.getenv("ANALYZE_MAX_WORDS", "260"))
LLM_STARTUP_DIAGNOSTICS: dict[str, Any] = {
    "status": "never_attempted",
    "elapsed_sec": None,
    "model": os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:3b"),
}
ONEDRIVE_AUTH_SESSIONS: dict[str, dict[str, Any]] = {}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Executes a Python module as a subprocess and captures its output.
# Returns a dictionary containing the return code, stdout, and stderr.
def run_module(args: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _build_onedrive_auth_config() -> dict[str, str]:
    client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError("ONEDRIVE_CLIENT_ID is required.")

    tenant = os.environ.get("ONEDRIVE_TENANT", "consumers").strip() or "consumers"
    token_cache_path = os.environ.get(
        "ONEDRIVE_TOKEN_CACHE",
        str(Path(default_state_dir()) / "token_cache.bin"),
    )
    return {
        "client_id": client_id,
        "tenant": tenant,
        "token_cache_path": token_cache_path,
    }


def _parse_onedrive_sync_counts(stdout: str) -> dict[str, int]:
    downloaded_new = 0
    downloaded_modified = 0
    checked_files = 0
    downloaded_files: list[str] = []
    folder_checked = ""
    user_checked = ""
    recursive_enabled = ""

    for line in stdout.split("\n"):
        clean_line = line.strip()
        if "Downloaded new files:" in line:
            try:
                downloaded_new = int(line.split("Downloaded new files:")[-1].strip())
            except (ValueError, IndexError):
                pass
        if "Downloaded modified files:" in line:
            try:
                downloaded_modified = int(line.split("Downloaded modified files:")[-1].strip())
            except (ValueError, IndexError):
                pass
        if "Checked Excel files:" in line:
            try:
                checked_files = int(line.split("Checked Excel files:")[-1].strip())
            except (ValueError, IndexError):
                pass
        if "Downloaded files:" in line:
            raw_names = line.split("Downloaded files:")[-1].strip()
            if raw_names and raw_names != "(none)":
                downloaded_files = [name.strip() for name in raw_names.split("|") if name.strip()]
            else:
                downloaded_files = []
        if "OneDrive folder checked:" in line:
            folder_checked = line.split("OneDrive folder checked:")[-1].strip()
        if "OneDrive user checked:" in line:
            user_checked = line.split("OneDrive user checked:")[-1].strip()
        if "Recursive enabled:" in line:
            recursive_enabled = line.split("Recursive enabled:")[-1].strip()

    return {
        "new": downloaded_new,
        "modified": downloaded_modified,
        "checked": checked_files,
        "downloaded_files": downloaded_files,
        "folder_checked": folder_checked,
        "user_checked": user_checked,
        "recursive_enabled": recursive_enabled,
    }


def _parse_google_sync_counts(stdout: str) -> dict[str, int]:
    counts = {
        "downloaded_or_updated": 0,
        "skipped_unchanged": 0,
        "skipped_folders": 0,
        "uploaded": 0,
    }
    patterns = {
        "downloaded_or_updated": r"Downloaded/updated:\s*(\d+)",
        "skipped_unchanged": r"skipped unchanged:\s*(\d+)",
        "skipped_folders": r"skipped folders:\s*(\d+)",
        "uploaded": r"uploaded:\s*(\d+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, stdout)
        if match:
            counts[key] = int(match.group(1))

    return counts


def clear_directory(dir_path: Path) -> int:
    if not dir_path.exists():
        return 0

    removed = 0
    for item in dir_path.iterdir():
        if item.is_file():
            item.unlink()
            removed += 1
        elif item.is_dir():
            removed += sum(1 for p in item.rglob("*") if p.is_file())
            shutil.rmtree(item)

    return removed


@app.on_event("startup")
def ensure_prompt_storage_on_startup() -> None:
    ensure_prompt_directories()


# API endpoint to check if the service is running.
# Returns a simple status message.
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/llm/startup-diagnostics")
def llm_startup_diagnostics() -> dict[str, Any]:
    """Return current LLM warmup diagnostics for optional frontend display."""
    diag = dict(LLM_STARTUP_DIAGNOSTICS)

    # Attempt warmup status refresh so diagnostics are informative,
    # but always return cached values on any unexpected error.
    try:
        warmup_result = warmup_llm(force=False)
        if isinstance(warmup_result, dict):
            if "status" in warmup_result:
                diag["status"] = warmup_result.get("status")
            if "elapsed_sec" in warmup_result:
                diag["elapsed_sec"] = warmup_result.get("elapsed_sec")
            if "model" in warmup_result and warmup_result.get("model"):
                diag["model"] = warmup_result.get("model")
            LLM_STARTUP_DIAGNOSTICS.update(diag)
    except Exception:
        pass

    return diag


def _resolve_prompt_record_path(record_file: str) -> tuple[Path | None, str | None]:
    prompts_dir, _ = ensure_prompt_directories()
    candidate = Path(record_file)
    if not candidate.is_absolute():
        candidate = (prompts_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if candidate.parent != prompts_dir:
        return None, "Response file must be in /data/responses (archive excluded)."
    if not candidate.exists() or not candidate.is_file():
        return None, "Response file not found."
    if candidate.suffix.lower() != ".json":
        return None, "Response file must be a JSON file."

    return candidate, None


def _run_prompt_auto_handle(record_path: Path, mode: str = "preview") -> dict[str, Any]:
    mode_value = str(mode or "preview").strip().lower()
    if mode_value not in {"preview", "apply"}:
        mode_value = "preview"

    module_result = run_module([
        "backend.src.prompt_auto_handle",
        "--record-file",
        str(record_path),
        "--mode",
        mode_value,
    ])

    if module_result.get("returncode", 1) != 0:
        return {
            "status": "error",
            "message": module_result.get("stderr", "Response transformer failed."),
            "transform_stdout": module_result.get("stdout", ""),
        }

    stdout = module_result.get("stdout", "").strip()
    if not stdout:
        return {
            "status": "error",
            "message": "Response transformer returned no output.",
        }

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Response transformer output was not valid JSON.",
            "transform_stdout": stdout,
        }

    if not isinstance(payload, dict):
        return {
            "status": "error",
            "message": "Response transformer output must be a JSON object.",
        }

    return payload


def _run_ai_response_append(record_path: Path, dry_run: bool = False) -> dict[str, Any]:
    """
    Load the record JSON, extract config path and LLM response,
    then call AI_response_append.append_mappings_to_config() directly.
    """
    try:
        record_data = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to load record file: {str(e)}",
        }

    # Extract config target from top-level (new slim format) or legacy input block.
    input_data = record_data.get("input") if isinstance(record_data.get("input"), dict) else {}
    config_file_path = (
        record_data.get("config_file_path")
        or record_data.get("config_file_name")
        or input_data.get("config_file_path")
        or input_data.get("config_file_name")
    )

    if not config_file_path:
        return {
            "status": "error",
            "message": "Record does not contain config_file_path or config_file_name.",
        }

    # Extract LLM response (handle both old dict format and new string format).
    llm_response = record_data.get("llm_response")
    if isinstance(llm_response, dict):
        response_text = llm_response.get("response", "")
    elif isinstance(llm_response, str):
        response_text = llm_response
    else:
        response_text = ""

    if not response_text or not response_text.strip():
        return {
            "status": "error",
            "message": "Record does not contain valid llm_response.",
        }

    # Import and call the append function directly.
    try:
        from backend.src.AI_response_append import append_mappings_to_config
        result_str = append_mappings_to_config(config_file_path, response_text)
        result = json.loads(result_str)
        result["status"] = "ok" if result.get("success") else "error"
        return result
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to append mappings: {str(e)}",
        }


def _extract_record_append_context(record_path: Path) -> dict[str, Any]:
    """
    Load a response record and extract config path + raw LLM response text.
    """
    try:
        record_data = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as error:
        return {
            "status": "error",
            "message": f"Failed to load record file: {error}",
        }

    input_data = record_data.get("input") if isinstance(record_data.get("input"), dict) else {}
    config_file_path = (
        record_data.get("config_file_path")
        or record_data.get("config_file_name")
        or input_data.get("config_file_path")
        or input_data.get("config_file_name")
    )
    if not config_file_path:
        return {
            "status": "error",
            "message": "Record does not contain config_file_path or config_file_name.",
        }

    llm_response = record_data.get("llm_response")
    if isinstance(llm_response, dict):
        response_text = llm_response.get("response", "")
    elif isinstance(llm_response, str):
        response_text = llm_response
    else:
        response_text = ""

    if not response_text or not response_text.strip():
        return {
            "status": "error",
            "message": "Record does not contain valid llm_response.",
        }

    return {
        "status": "ok",
        "config_file_path": config_file_path,
        "response_text": response_text,
    }


@app.get("/responses/pending")
def prompts_pending() -> dict[str, Any]:
    prompts_dir, archive_dir = ensure_prompt_directories()
    records: list[dict[str, Any]] = []

    for prompt_file in sorted(prompts_dir.glob("*.json"), key=lambda p: p.name.lower()):
        try:
            raw = json.loads(prompt_file.read_text(encoding="utf-8"))
        except Exception:
            raw = {}

        input_data = raw.get("input") if isinstance(raw.get("input"), dict) else {}
        llm_response = raw.get("llm_response")
        
        # Handle both old format (dict with "response" key) and new format (string)
        if isinstance(llm_response, dict):
            response_text = llm_response.get("response", "")
        else:
            response_text = str(llm_response) if llm_response else ""
        
        prompt_text = raw.get("prompt") if isinstance(raw.get("prompt"), str) else ""

        records.append(
            {
                "record_id": raw.get("record_id") or prompt_file.stem,
                "record_file": str(prompt_file.resolve()),
                "type": raw.get("type", "unknown"),
                "status": raw.get("status", "unknown"),
                "created_at_utc": raw.get("created_at_utc"),
                "input_file_name": raw.get("input_file_name") or input_data.get("input_file_name"),
                "config_file_name": raw.get("config_file_name") or input_data.get("config_file_name"),
                "prompt_preview": prompt_text,
                "response_preview": response_text,
                "has_response": bool(response_text),
            }
        )

    return {
        "status": "ok",
        "count": len(records),
        "responses": records,
        "responses_dir": str(prompts_dir.resolve()),
        "archive_dir": str(archive_dir.resolve()),
    }

@app.post("/responses/archive")
def prompts_archive(data: dict = Body(...)) -> dict[str, Any]:
    record_file = str(data.get("record_file") or "").strip()
    if not record_file:
        return {
            "status": "error",
            "message": "record_file is required.",
        }

    resolved, error_message = _resolve_prompt_record_path(record_file)
    if error_message:
        return {
            "status": "error",
            "message": error_message,
        }

    archive_result = archive_prompt_record(str(resolved))
    return {
        "status": "ok" if archive_result.get("archived") else "error",
        **archive_result,
    }


@app.post("/responses/auto-handle/preview")
def prompts_auto_handle_preview(data: dict = Body(...)) -> dict[str, Any]:
    record_file = str(data.get("record_file") or "").strip()
    if not record_file:
        return {
            "status": "error",
            "message": "record_file is required.",
        }

    resolved, error_message = _resolve_prompt_record_path(record_file)
    if error_message:
        return {
            "status": "error",
            "message": error_message,
        }

    result = _run_prompt_auto_handle(resolved, mode="preview")
    if result.get("status") != "ok":
        return result
    return {
        **result,
        "record_file": str(resolved),
    }


@app.post("/responses/auto-handle/execute")
def prompts_auto_handle_execute(data: dict = Body(...)) -> dict[str, Any]:
    record_file = str(data.get("record_file") or "").strip()
    mode = str(data.get("mode") or "preview").strip().lower()
    if not record_file:
        return {
            "status": "error",
            "message": "record_file is required.",
        }

    resolved, error_message = _resolve_prompt_record_path(record_file)
    if error_message:
        return {
            "status": "error",
            "message": error_message,
        }

    result = _run_prompt_auto_handle(resolved, mode=mode)
    if result.get("status") != "ok":
        return result

    append_result: dict[str, Any] | None = None
    if mode == "apply":
        append_result = _run_ai_response_append(resolved, dry_run=False)
        if append_result.get("status") != "ok":
            return {
                "status": "error",
                "message": "Response transformer succeeded but mapping append failed.",
                "record_file": str(resolved),
                "auto_handle_result": result,
                "append_result": append_result,
            }

    response_payload = {
        **result,
        "record_file": str(resolved),
    }
    if append_result is not None:
        response_payload["append_result"] = append_result

    return response_payload


# API endpoint to run the Excel validation script.
# Calls backend.src.validate_excel_files module with optional hash reset flag.

# API endpoint to delete output files, hash files, and reset OneDrive manifest.
# Clears all processed data and forces fresh download/validation on next run.
@app.post("/run/cleanup")
def run_cleanup() -> dict[str, Any]:
    removed = {}

    # Keep input folder intact; reset all generated state around it.
    output_dir = Path(default_output_dir())
    hash_dir = Path(default_hash_dir())
    state_dir = Path(default_state_dir())
    archive_dir = Path(default_archive_dir())
    token_cache_path = Path(
        os.environ.get(
            "ONEDRIVE_TOKEN_CACHE",
            str(state_dir / "token_cache.bin"),
        )
    )
    token_cache_snapshot: Optional[bytes] = None
    if token_cache_path.is_file():
        try:
            token_cache_snapshot = token_cache_path.read_bytes()
            removed["onedrive_auth_snapshot"] = True
        except Exception as exc:
            logger.warning("Could not snapshot OneDrive token cache before cleanup: %s", exc)
            removed["onedrive_auth_snapshot"] = False
    else:
        removed["onedrive_auth_snapshot"] = False

    prompts_dir, prompts_archive_dir = ensure_prompt_directories()

    removed["output_removed"] = clear_directory(output_dir)
    removed["hashes_removed"] = clear_directory(hash_dir)
    removed["state_removed"] = clear_directory(state_dir)
    removed["archive_removed"] = clear_directory(archive_dir)

    if token_cache_snapshot is not None:
        try:
            token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            token_cache_path.write_bytes(token_cache_snapshot)
            removed["onedrive_auth_restored"] = True
        except Exception as exc:
            removed["onedrive_auth_restored"] = False
            logger.warning("Could not restore OneDrive token cache after cleanup: %s", exc)
    else:
        removed["onedrive_auth_restored"] = False

    removed["prompts_archive_removed"] = clear_directory(prompts_archive_dir)
    removed["prompts_removed"] = clear_directory(prompts_dir)
    ensure_prompt_directories()

    manifest_path = Path(os.environ.get("ONEDRIVE_MANIFEST", str(Path(default_state_dir()) / "onedrive_manifest.xlsx")))
    if manifest_path.exists():
        manifest_path.unlink()
        removed["manifest_deleted"] = True
    else:
        removed["manifest_deleted"] = False

    total_removed = (
        removed["output_removed"]
        + removed["hashes_removed"]
        + removed["state_removed"]
        + removed["archive_removed"]
        + removed["prompts_archive_removed"]
        + removed["prompts_removed"]
        + (1 if removed["manifest_deleted"] else 0)
    )
    return {
        "status": "ok",
        "message": (
            f"✓ Cleanup complete ({total_removed} items removed). "
            "Input folder was preserved."
        ),
        **removed,
    }


# API endpoint to sync changed Excel files from OneDrive.
# Downloads new/modified files and updates the manifest.
@app.post("/run/onedrive_download")
def run_onedrive_download() -> dict[str, Any]:
    result = run_module(["backend.src.onedrive_download"])
    counts = _parse_onedrive_sync_counts(result.get("stdout", ""))
    download_dir = os.environ.get("ONEDRIVE_DOWNLOAD_DIR", default_input_dir())
    token_cache_path = os.environ.get(
        "ONEDRIVE_TOKEN_CACHE",
        str(Path(default_state_dir()) / "token_cache.bin"),
    )
    manifest_path = os.environ.get(
        "ONEDRIVE_MANIFEST",
        str(Path(default_state_dir()) / "onedrive_manifest.xlsx"),
    )

    if result.get("returncode", 1) == 0:
        result["status"] = "ok"
        downloaded_files = counts.get("downloaded_files", [])
        downloaded_summary = ", ".join(downloaded_files) if downloaded_files else "(none)"
        result["message"] = (
            "✓ OneDrive sync complete\n"
            f"Folder checked: {counts.get('folder_checked') or os.environ.get('ONEDRIVE_REMOTE_FOLDER', '/')}\n"
            f"User checked: {counts.get('user_checked') or 'unknown'}\n"
            f"Recursive: {counts.get('recursive_enabled') or 'unknown'}\n"
            f"Total eligible files detected: {counts.get('checked', 0)}\n"
            f"New: {counts['new']}, Modified: {counts['modified']}\n"
            f"Downloaded files: {downloaded_summary}\n"
            f"Saved to: {download_dir}"
        )
    else:
        result["status"] = "error"
        result["message"] = (
            "OneDrive sync failed. "
            "Open debug output for details and re-authenticate if prompted."
        )

    result["sync_counts"] = counts
    result["download_dir"] = download_dir
    result["token_cache_path"] = token_cache_path
    result["manifest_path"] = manifest_path
    return result


@app.post("/run/onedrive_auth/start")
def run_onedrive_auth_start() -> dict[str, Any]:
    try:
        cfg = _build_onedrive_auth_config()
        token_cache_path = Path(cfg["token_cache_path"])
        cache = load_token_cache(token_cache_path)
        msal_app = msal.PublicClientApplication(
            client_id=cfg["client_id"],
            authority=f"https://login.microsoftonline.com/{cfg['tenant']}",
            token_cache=cache,
        )

        scopes = ["Files.Read.All"]
        accounts = msal_app.get_accounts()
        if accounts:
            silent_result = msal_app.acquire_token_silent(scopes=scopes, account=accounts[0])
            if silent_result and "access_token" in silent_result:
                save_token_cache(cache, token_cache_path)
                return {
                    "status": "ok",
                    "message": "OneDrive is already authenticated.",
                    "already_authenticated": True,
                }

        flow = msal_app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            return {
                "status": "error",
                "message": f"Failed to start OneDrive device authentication: {flow}",
            }

        auth_id = uuid.uuid4().hex
        ONEDRIVE_AUTH_SESSIONS[auth_id] = {
            "app": msal_app,
            "cache": cache,
            "flow": flow,
            "token_cache_path": token_cache_path,
            "created_at": int(time.time()),
        }

        return {
            "status": "pending",
            "message": flow.get("message", "Authenticate with Microsoft using the provided code."),
            "auth_id": auth_id,
            "user_code": flow.get("user_code"),
            "verification_uri": flow.get("verification_uri"),
            "verification_uri_complete": flow.get("verification_uri_complete"),
            "expires_in": flow.get("expires_in"),
            "interval": flow.get("interval"),
            "already_authenticated": False,
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Could not start OneDrive authentication: {exc}",
        }


@app.post("/run/onedrive_auth/complete")
def run_onedrive_auth_complete(auth_id: str = Body(..., embed=True)) -> dict[str, Any]:
    session = ONEDRIVE_AUTH_SESSIONS.get(auth_id)
    if not session:
        return {
            "status": "error",
            "message": "Authentication session expired or not found. Start authentication again.",
        }

    flow = session["flow"]
    app_obj = session["app"]
    cache = session["cache"]
    token_cache_path = session["token_cache_path"]

    # Short timeout keeps API responsive while still allowing a completion click.
    result = app_obj.acquire_token_by_device_flow(flow, timeout=3)
    error_code = result.get("error")

    if "access_token" in result:
        save_token_cache(cache, token_cache_path)
        ONEDRIVE_AUTH_SESSIONS.pop(auth_id, None)
        return {
            "status": "ok",
            "message": "OneDrive authentication successful. You can now run OneDrive sync from chat.",
        }

    if error_code in {"authorization_pending", "slow_down"}:
        return {
            "status": "pending",
            "message": "Waiting for Microsoft sign-in to finish. Complete browser sign-in, then click complete again.",
        }

    ONEDRIVE_AUTH_SESSIONS.pop(auth_id, None)
    return {
        "status": "error",
        "message": result.get("error_description", "OneDrive authentication failed."),
    }


@app.post("/run/file-sync")
def run_file_sync() -> dict[str, Any]:
    google_result = run_module(["backend.src.google_to_onedrive_sync"])
    google_counts = _parse_google_sync_counts(google_result.get("stdout", ""))

    if google_result.get("returncode", 1) != 0:
        return {
            "status": "error",
            "message": "File sync stopped: Google Drive -> OneDrive sync failed.",
            "google_to_onedrive": {
                **google_result,
                "sync_counts": google_counts,
            },
        }

    onedrive_result = run_module(["backend.src.onedrive_download"])
    onedrive_counts = _parse_onedrive_sync_counts(onedrive_result.get("stdout", ""))

    status_text = "ok" if onedrive_result.get("returncode", 1) == 0 else "error"
    return {
        "status": status_text,
        "message": (
            "✓ File sync complete - "
            f"Google updated: {google_counts['downloaded_or_updated']}, "
            f"OneDrive new: {onedrive_counts['new']}, OneDrive modified: {onedrive_counts['modified']}"
        ) if status_text == "ok" else "File sync partially failed: OneDrive -> project sync failed.",
        "google_to_onedrive": {
            **google_result,
            "sync_counts": google_counts,
        },
        "onedrive_to_project": {
            **onedrive_result,
            "sync_counts": onedrive_counts,
        },
    }




# Inlined file processing logic (formerly in backend/src/file_processing.py)
# Inline process_file logic (formerly from transforming_excel.py)
def process_file(input_path, output_dir):
    output_path = output_dir / input_path.name
    try:
        # For Excel and binary files, just copy them (don't transform as text)
        if input_path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
            output_path.write_bytes(input_path.read_bytes())
            return output_path
        # For text-based files, try to read and transform
        content = input_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        output_path.write_bytes(input_path.read_bytes())
        return output_path
    # Placeholder for LLM-based transform; swap in API call later.
    transformed = content
    output_path.write_text(transformed, encoding="utf-8")
    return output_path

def process_all_input_files_and_update_manifest():
    """
    Scans input directory, detects changed files, processes them, and updates the changed files manifest.
    Returns a tuple: (all_files, changed_files, unchanged_files)
    """
    input_dir = Path(default_input_dir())
    output_dir = Path(default_output_dir())
    hash_dir = Path(default_hash_dir())
    all_files = []
    for pattern in ("*.xlsx", "*.xls", "*.xlsm", "*.csv"):
        all_files.extend(input_dir.glob(pattern))
    all_files = sorted([f.name for f in all_files])

    changed_files = []
    for file_path in input_dir.iterdir():
        if not file_path.is_file() or file_path.suffix.lower() not in {".xlsx", ".xls", ".xlsm", ".csv"}:
            continue
        preview = compare_file_without_updating(str(file_path))
        state = preview.get("status")
        if state in {"first_version", "changed"}:
            try:
                process_file(file_path, output_dir)
                changed_files.append(file_path.name)
            except Exception:
                continue
    unchanged_files = [f for f in all_files if f not in changed_files]
    # Update manifest
    manifest_path = Path(default_changed_files_path())
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(changed_files), encoding="utf-8")
    return all_files, changed_files, unchanged_files

@app.post("/run/file-processing")
def run_file_processing(reset_hashes: bool = False) -> dict[str, Any]:
    # Optionally reset hashes (if needed, implement here)
    # (Legacy: reset_hashes logic can be added if required)

    all_files, changed_files, unchanged_files = process_all_input_files_and_update_manifest()
    status_text = "ok"
    files_section = ""
    if all_files:
        files_section = "\n\nFiles scanned:\n"
        files_section += f"  • Total: {len(all_files)}\n"
        files_section += f"  • Changed (prepared for LLM): {len(changed_files)}\n"
        files_section += f"  • Unchanged: {len(unchanged_files)}\n"
    if changed_files:
        files_section += f"\nPrepared files for analysis:\n"
        for fname in changed_files:
            files_section += f"  • {fname}\n"
    elif all_files:
        files_section += f"\nNo changed files detected."

    detailed_message = (
        "✓ File processing complete - "
        f"Scanned {len(all_files)}, Found {len(changed_files)} changed"
        f"{files_section}"
    )

    return {
        "status": status_text,
        "message": detailed_message,
        "file_details": {
            "all_files": all_files,
            "changed_files": changed_files,
            "unchanged_files": unchanged_files,
            "total": len(all_files),
            "changed_count": len(changed_files),
            "unchanged_count": len(unchanged_files),
        },
    }


@app.post("/run/extract-columns")
def run_extract_columns(file_name: str | None = None) -> dict[str, Any]:
    input_dir = Path(default_input_dir())
    input_dir.mkdir(parents=True, exist_ok=True)

    if not file_name:
        excel_files = []
        for pattern in ("*.xlsx", "*.xls", "*.xlsm"):
            excel_files.extend(input_dir.glob(pattern))

        if not excel_files:
            return {
                "status": "error",
                "message": "No Excel files found in input folder. Upload one first.",
            }

        if len(excel_files) > 1:
            file_names = ", ".join(sorted(file_path.name for file_path in excel_files))
            return {
                "status": "error",
                "message": (
                    "Multiple Excel files found. Provide file_name explicitly via /run/extract-columns "
                    f"or keep only one file in input. Found: {file_names}"
                ),
            }

        target_file = excel_files[0]
    else:
        target_file = input_dir / file_name

    if not target_file.exists():
        return {
            "status": "error",
            "message": f"File not found in input folder: {target_file.name}",
        }

    if target_file.suffix.lower() not in {".xlsx", ".xls", ".xlsm"}:
        return {
            "status": "error",
            "message": f"Unsupported file type: {target_file.suffix}. Please provide an Excel file.",
        }

    try:
        payload = json.loads(extract_columns(str(target_file)))
    except Exception as error:
        return {
            "status": "error",
            "message": f"Failed to extract columns: {error}",
        }

    total_columns = sum(
        len(sheet_data.get("column_names", []))
        for sheet_data in payload.values()
        if isinstance(sheet_data, dict)
    )
    return {
        "status": "ok",
        "message": f"Extracted {total_columns} columns from {target_file.name}",
        "file_name": target_file.name,
        "column_data": payload,
    }




# @app.post("/extract-columns-file")
# async def extract_columns_file(file: UploadFile = File(...)) -> dict[str, Any]:
#     file_name = file.filename or "uploaded_file"
#     file_ext = Path(file_name).suffix.lower()
#     if file_ext not in {".xlsx", ".xls", ".xlsm"}:
#         return {
#             "status": "error",
#             "message": f"Unsupported file type: {file_ext}. Please provide an Excel file.",
#         }
#
#     temp_file_path: Path | None = None
#     try:
#         with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
#             temp_file.write(await file.read())
#             temp_file_path = Path(temp_file.name)
#
#         payload = json.loads(extract_columns(str(temp_file_path)))
#         total_columns = sum(
#             len(sheet_data.get("column_names", []))
#             for sheet_data in payload.values()
#             if isinstance(sheet_data, dict)
#         )
#         return {
#             "status": "ok",
#             "message": f"Extracted {total_columns} columns from {file_name}",
#             "file_name": file_name,
#             "column_data": payload,
#         }
#     except Exception as error:
#         return {
#             "status": "error",
#             "message": f"Failed to extract columns: {error}",
#         }
#     finally:
#         if temp_file_path and temp_file_path.exists():
#             temp_file_path.unlink()


@app.post("/run/append-mappings")
def run_append_mappings(record_file: str | None = None, config_file_name: str | None = None) -> dict[str, Any]:
    prompts_dir, _ = ensure_prompt_directories()
    prompts_dir.mkdir(parents=True, exist_ok=True)

    if not record_file:
        response_files = list(prompts_dir.glob("*.json"))

        if not response_files:
            return {
                "status": "error",
                "message": "No response records found in responses folder. Generate one first.",
            }

        if len(response_files) > 1:
            file_names = ", ".join(sorted(file_path.name for file_path in response_files))
            return {
                "status": "error",
                "message": (
                    "Multiple response records found. Provide record_file explicitly via /run/append-mappings "
                    f"or keep only one in responses folder. Found: {file_names}"
                ),
            }

        target_record = response_files[0]
    else:
        target_record = prompts_dir / record_file

    if not target_record.exists():
        return {
            "status": "error",
            "message": f"Record file not found: {target_record.name}",
        }

    if target_record.suffix.lower() != ".json":
        return {
            "status": "error",
            "message": f"Unsupported file type: {target_record.suffix}. Please provide a JSON response record.",
        }

    try:
        result = _run_ai_response_append(target_record, dry_run=False)
    except Exception as error:
        return {
            "status": "error",
            "message": f"Failed to append mappings: {error}",
        }

    if result.get("status") != "ok":
        return result

    return {
        "status": "ok",
        "message": f"Appended {result.get('rows_appended', 0)} rows to config file",
        "record_file": target_record.name,
        "append_result": result,
    }

@app.post("/responses/append-mappings")
async def responses_append_mappings(request: Request) -> dict[str, Any]:
    # Endpoint for frontend: handles append mappings for response records
    # Accepts JSON body with record_file parameter
    try:
        data = await request.json() or {}
        record_file = data.get("record_file")
        if not record_file:
            return {
                "status": "error",
                "message": "record_file is required in request body.",
            }
        return run_append_mappings(record_file=record_file)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to append mappings: {str(e)}",
        }


@app.post("/responses/smart-update/preview")
async def responses_smart_update_preview(request: Request) -> dict[str, Any]:
    try:
        data = await request.json() or {}
        record_file = str(data.get("record_file") or "").strip()
        fuzzy_threshold = float(data.get("fuzzy_threshold") or 0.85)

        if not record_file:
            return {
                "status": "error",
                "message": "record_file is required in request body.",
            }

        resolved, error_message = _resolve_prompt_record_path(record_file)
        if error_message:
            return {
                "status": "error",
                "message": error_message,
            }

        context = _extract_record_append_context(resolved)
        if context.get("status") != "ok":
            return context

        from backend.src.AI_response_append import build_smart_update_preview

        preview = build_smart_update_preview(
            context["config_file_path"],
            context["response_text"],
            fuzzy_threshold=fuzzy_threshold,
        )

        if not preview.get("success"):
            return {
                "status": "error",
                "message": preview.get("error") or "Smart update preview failed.",
                "preview": preview,
                "record_file": str(resolved),
            }

        return {
            "status": "ok",
            "message": "Smart update preview ready.",
            "record_file": str(resolved),
            "preview": preview,
        }
    except Exception as error:
        return {
            "status": "error",
            "message": f"Smart update preview failed: {error}",
        }


@app.post("/responses/smart-update/apply")
async def responses_smart_update_apply(request: Request) -> dict[str, Any]:
    try:
        data = await request.json() or {}
        record_file = str(data.get("record_file") or "").strip()
        fuzzy_threshold = float(data.get("fuzzy_threshold") or 0.85)
        accepted_changes = data.get("accepted_changes")

        if not record_file:
            return {
                "status": "error",
                "message": "record_file is required in request body.",
            }
        if not isinstance(accepted_changes, list):
            return {
                "status": "error",
                "message": "accepted_changes must be a list.",
            }

        resolved, error_message = _resolve_prompt_record_path(record_file)
        if error_message:
            return {
                "status": "error",
                "message": error_message,
            }

        context = _extract_record_append_context(resolved)
        if context.get("status") != "ok":
            return context

        from backend.src.AI_response_append import apply_smart_update_changes

        apply_result = apply_smart_update_changes(
            context["config_file_path"],
            context["response_text"],
            accepted_changes=accepted_changes,
            fuzzy_threshold=fuzzy_threshold,
        )

        if not apply_result.get("success"):
            return {
                "status": "error",
                "message": apply_result.get("error") or "Smart update apply failed.",
                "apply_result": apply_result,
                "record_file": str(resolved),
            }

        return {
            "status": "ok",
            "message": "Smart update changes applied.",
            "record_file": str(resolved),
            "apply_result": apply_result,
        }
    except Exception as error:
        return {
            "status": "error",
            "message": f"Smart update apply failed: {error}",
        }


@app.post("/run/process-input-folder")
def process_input_folder_with_ai() -> dict[str, Any]:
    input_dir = Path(default_input_dir())
    input_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for pattern in ("*.xlsx", "*.xls", "*.xlsm", "*.csv"):
        files.extend(input_dir.glob(pattern))

    if not files:
        return {
            "status": "error",
            "message": "No supported files found in input folder.",
            "results": [],
        }

    results = []

    for file_path in files:
        temp_excel_path: Path | None = None

        try:
            file_ext = file_path.suffix.lower()

            if file_ext == ".csv":
                df = pd.read_csv(file_path)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp_file:
                    temp_excel_path = Path(temp_file.name)

                df.to_excel(temp_excel_path, index=False)
                payload = json.loads(extract_columns(str(temp_excel_path)))
            else:
                payload = json.loads(extract_columns(str(file_path)))

            prompt = f"""You are analyzing extracted column information from a data file.

File name: {file_path.name}

Extracted column information:
{json.dumps(payload, indent=2)}

Please provide:
1. A short summary of the columns
2. Possible meanings of the columns
3. Any inconsistencies or problems you notice
4. Suggestions for mapping or standardizing the fields
"""

            logger.info("Sending %s to AI", file_path.name)
            llm_result = query_llm(prompt)

            if "error" in llm_result:
                results.append({
                    "file": file_path.name,
                    "status": "error",
                    "message": llm_result["error"],
                })
            else:
                results.append({
                    "file": file_path.name,
                    "status": "ok",
                    "column_data": payload,
                    "ai_response": llm_result.get("response", ""),
                })

        except Exception as e:
            logger.exception("Failed processing file %s", file_path.name)
            results.append({
                "file": file_path.name,
                "status": "error",
                "message": str(e),
            })
        finally:
            if temp_excel_path and temp_excel_path.exists():
                temp_excel_path.unlink()

    return {
        "status": "ok",
        "message": f"Processed {len(files)} file(s) from input folder",
        "results": results,
    }


# API endpoint to upload a file to the input folder and validate & transform it.
# Accepts Excel (.xlsx, .xls, .xlsm) or CSV files.
# Returns validation and transformation results.
@app.post("/upload_and_process")
async def upload_and_process(file: UploadFile = File(...)) -> dict[str, Any]:
    """
    Upload a file to the input folder and run validation + transformation.
    """
    try:
        # Validate file type
        file_name = file.filename or "uploaded_file"
        valid_extensions = {".xlsx", ".xls", ".xlsm", ".csv"}
        file_ext = Path(file_name).suffix.lower()

        if file_ext not in valid_extensions:
            return {
                "status": "error",
                "message": f"Invalid file type: {file_ext}. Supported: {', '.join(valid_extensions)}"
            }

        # Save file to input folder
        input_dir = Path(default_input_dir())
        input_dir.mkdir(parents=True, exist_ok=True)
        file_path = input_dir / file_name

        # Read and save file (overwrites if exists)
        contents = await file.read()
        file_path.write_bytes(contents)

        # Preview changes without updating baseline; baseline is updated later
        # after confirmed analysis/processing.
        change_results = compare_file_without_updating(file_name)

        return {
            "status": "ok",
            "message": f"File uploaded: {file_name}",
            "file_path": str(file_path),
            "change_results": change_results,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Upload failed: {str(e)}"
        }


# Compare changes and then save this uploaded file as the new baseline.
@app.post("/check_file_changes")
def check_file_changes(data: dict = Body(...)) -> dict[str, Any]:
    file_name = data.get("file_name")
    update_baseline = bool(data.get("update_baseline", False))

    if not file_name:
        return {
            "status": "error",
            "message": "No file name was provided."
        }

    try:
        logger.info("CHECK_FILE_CHANGES HIT for file: %s", file_name)

        result = compare_file_without_updating(file_name)

        logger.info("Check changes preview result for %s: %s", file_name, result.get("status"))

        # Only persist baseline when explicitly requested by caller.
        if update_baseline and result.get("status") == "changed":
            save_result = save_current_file_as_new_version(file_name)
            logger.info("Saved new baseline for %s: %s", file_name, save_result.get("status"))
            result["baseline_updated"] = save_result.get("status") == "ok"
        else:
            result["baseline_updated"] = False

        return result
    except Exception as e:
        logger.exception("Check changes failed for file: %s", file_name)
        return {
            "status": "error",
            "message": str(e)
        }


# API endpoint to sync files from Google Drive to the OneDrive backup folder.
@app.post("/run/google_to_onedrive")
def run_google_to_onedrive() -> dict[str, Any]:
    # Backward-compatible alias for the unified file sync command.
    return run_file_sync()


def _config_dir() -> Path:
    # Hard-coded config location for now.
    config_dir = ANALYZE_CONFIG_DIR
    if str(config_dir).startswith("/data") and not Path("/data").exists():
        config_dir = PROJECT_ROOT / "data" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _list_input_candidates() -> list[Path]:
    input_dir = Path(default_input_dir())
    if not input_dir.exists():
        return []
    return sorted(
        [
            path for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in ANALYZE_VALID_EXTENSIONS
        ],
        key=lambda path: path.name.lower(),
    )


def _list_config_candidates() -> list[Path]:
    config_dir = _config_dir()
    return sorted(
        [
            path for path in config_dir.iterdir()
            if path.is_file() and path.suffix.lower() in ANALYZE_VALID_EXTENSIONS
        ],
        key=lambda path: path.name.lower(),
    )


def _is_config_candidate(path: Path) -> bool:
    return "config" in path.stem.lower()


def _resolve_by_name(candidates: list[Path], file_name: str | None) -> Path | None:
    if not file_name:
        return None
    file_name = file_name.strip()
    if not file_name:
        return None

    for path in candidates:
        if path.name == file_name:
            return path
    for path in candidates:
        if path.name.lower() == file_name.lower():
            return path
    return None


def _read_default_config_name() -> str | None:
    value = os.environ.get(ANALYZE_DEFAULT_CONFIG_ENV_KEY, "").strip()
    return value or None


def _save_default_config_name(file_name: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    key = ANALYZE_DEFAULT_CONFIG_ENV_KEY
    line = f"{key}={file_name}"

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    replaced = False
    for index, current in enumerate(lines):
        if current.startswith(f"{key}="):
            lines[index] = line
            replaced = True
            break

    if not replaced:
        lines.append(line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = file_name


def _resolve_config_file_path(file_name: str | None) -> str | None:
    if not file_name:
        return None

    candidate_name = str(file_name).strip()
    if not candidate_name:
        return None

    candidate = Path(candidate_name)
    if candidate.is_absolute() and candidate.exists():
        return str(candidate.resolve())

    resolved = _resolve_by_name(_list_config_candidates(), candidate_name)
    if resolved:
        return str(resolved.resolve())

    config_dir = _config_dir()
    fallback = (config_dir / candidate_name).resolve()
    if fallback.exists():
        return str(fallback)

    return None


def _build_saved_response_input(
    *,
    input_file_info: str,
    config_file_info: str,
    input_file_name: str,
    change_status: str,
    config_file_name: str | None = None,
) -> dict[str, Any]:
    input_data: dict[str, Any] = {
        "input_file_info": input_file_info,
        "config_file_info": config_file_info,
        "input_file_name": input_file_name,
        "change_status": change_status,
    }

    if config_file_name:
        input_data["config_file_name"] = config_file_name
        resolved_config_file = _resolve_config_file_path(config_file_name)
        if resolved_config_file:
            input_data["config_file_path"] = resolved_config_file

    return input_data


@app.get("/analyze/config-options")
def analyze_config_options() -> dict[str, Any]:
    config_dir = _config_dir()
    config_candidates = _list_config_candidates()

    if not config_candidates:
        return {
            "status": "none",
            "config_dir": str(config_dir),
            "message": "No config files found in data/config.",
            "config_options": [],
        }

    if len(config_candidates) == 1:
        return {
            "status": "single",
            "config_dir": str(config_dir),
            "selected_config": config_candidates[0].name,
            "config_options": [config_candidates[0].name],
        }

    return {
        "status": "multiple",
        "config_dir": str(config_dir),
        "config_options": [path.name for path in config_candidates],
        "message": "Multiple config files found in data/config. Choose one for this run and remove extras if possible.",
    }


@app.post("/upload_config")
async def upload_config(file: UploadFile = File(...)) -> dict[str, Any]:
    file_name = file.filename or "uploaded_config"
    file_ext = Path(file_name).suffix.lower()

    if file_ext not in ANALYZE_VALID_EXTENSIONS:
        return {
            "status": "error",
            "message": f"Invalid config file type: {file_ext}. Supported: {', '.join(sorted(ANALYZE_VALID_EXTENSIONS))}",
        }

    config_dir = _config_dir()
    config_path = config_dir / file_name

    try:
        contents = await file.read()
        if not contents:
            return {
                "status": "error",
                "message": "Uploaded config file is empty.",
            }

        config_path.write_bytes(contents)
        _save_default_config_name(file_name)

        return {
            "status": "ok",
            "message": f"Config saved to data/config: {file_name}",
            "config_name": file_name,
            "config_path": str(config_path),
        }
    except Exception as error:
        logger.exception("Config upload failed for %s", file_name)
        return {
            "status": "error",
            "message": f"Config upload failed: {error}",
        }


def _select_default_input(input_candidates: list[Path]) -> Path | None:
    if not input_candidates:
        return None
    non_config = [path for path in input_candidates if not _is_config_candidate(path)]
    pool = non_config or input_candidates
    return max(pool, key=lambda path: path.stat().st_mtime)


def _materialize_existing_as_excel(source_path: Path, temp_paths: list[Path]) -> tuple[Path, str]:
    suffix = source_path.suffix.lower()
    if suffix == ".csv":
        csv_df = pd.read_csv(source_path)
        excel_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        excel_path = Path(excel_tmp.name)
        excel_tmp.close()
        temp_paths.append(excel_path)
        csv_df.to_excel(excel_path, index=False)
        return excel_path, f"CSV converted to Excel for extract_columns ({len(csv_df.columns)} columns)"

    workbook = pd.ExcelFile(source_path)
    sheet_info = f"Excel with {len(workbook.sheet_names)} sheet(s): {', '.join(workbook.sheet_names)}"
    return source_path, sheet_info


def _materialize_uploaded_bytes_as_excel(
    original_name: str,
    raw_bytes: bytes,
    role: str,
    temp_paths: list[Path],
) -> tuple[Path, str]:
    suffix = Path(original_name).suffix.lower()
    if suffix not in ANALYZE_VALID_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type for {role}: {suffix}. Supported: {', '.join(sorted(ANALYZE_VALID_EXTENSIONS))}"
        )

    if suffix == ".csv":
        csv_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        csv_path = Path(csv_tmp.name)
        csv_tmp.write(raw_bytes)
        csv_tmp.close()
        temp_paths.append(csv_path)

        excel_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        excel_path = Path(excel_tmp.name)
        excel_tmp.close()
        temp_paths.append(excel_path)

        df = pd.read_csv(csv_path)
        df.to_excel(excel_path, index=False)
        return excel_path, f"CSV converted to Excel for extract_columns ({len(df.columns)} columns)"

    excel_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    excel_path = Path(excel_tmp.name)
    excel_tmp.write(raw_bytes)
    excel_tmp.close()
    temp_paths.append(excel_path)

    workbook = pd.ExcelFile(excel_path)
    sheet_info = f"Excel with {len(workbook.sheet_names)} sheet(s): {', '.join(workbook.sheet_names)}"
    return excel_path, sheet_info


def _summarize_change_detection(change_result: dict[str, Any]) -> str:
    sheet_changes = change_result.get("sheet_changes", {})
    added_sheets = sheet_changes.get("added_sheets", [])
    removed_sheets = sheet_changes.get("removed_sheets", [])
    updated_sheets = sheet_changes.get("updated_sheets", [])
    added_rows = len(change_result.get("added_rows", []))
    removed_rows = len(change_result.get("removed_rows", []))
    updated_rows = len(change_result.get("updated_rows", []))
    value_changes = len(change_result.get("value_changes", []))

    return (
        f"Sheets changed: +{len(added_sheets)} / -{len(removed_sheets)} / ~{len(updated_sheets)}; "
        f"Rows: +{added_rows} / -{removed_rows} / ~{updated_rows}; "
        f"Cell updates: {value_changes}"
    )


def _classify_change_impact(change_result: dict[str, Any]) -> dict[str, str]:
    status = str(change_result.get("status") or "").strip().lower()

    if status == "first_version":
        return {
            "level": "new",
            "label": "New file",
            "reason": "No previous baseline exists yet for this input file.",
        }

    if status == "no_change":
        return {
            "level": "none",
            "label": "No detected changes",
            "reason": "Input matches the latest saved baseline; zero row/sheet diffs are expected.",
        }

    sheet_changes = change_result.get("sheet_changes", {})
    added_sheets = len(sheet_changes.get("added_sheets", []))
    removed_sheets = len(sheet_changes.get("removed_sheets", []))
    columns_added = len(change_result.get("columns_added", []))
    columns_removed = len(change_result.get("columns_removed", []))
    updated_rows = len(change_result.get("updated_rows", []))
    value_changes = len(change_result.get("value_changes", []))

    if added_sheets or removed_sheets or columns_added or columns_removed:
        return {
            "level": "high",
            "label": "High impact",
            "reason": "Schema-level changes detected (sheet/column structure changed).",
        }

    if updated_rows > 25 or value_changes > 100:
        return {
            "level": "medium",
            "label": "Medium impact",
            "reason": "Substantial data-level updates detected.",
        }

    return {
        "level": "low",
        "label": "Low impact",
        "reason": "Small data-only changes detected.",
    }


def _build_analysis_confirmation_response(
    change_status: str,
    change_summary: str,
    selected_input: str,
    selected_config: str,
    change_impact: dict[str, str],
    change_detection: dict[str, Any],
) -> dict[str, Any]:
    reason = "Changes were detected in a previously processed input file." if change_status == "changed" else "No changes were detected in a previously processed input file."
    return {
        "status": "needs_analysis_confirmation",
        "message": f"{reason} Confirm analyze by sending confirm_ai_analysis=true. {change_summary}",
        "selected_input": selected_input,
        "selected_config": selected_config,
        "analysis_recommended": "yes" if change_status == "changed" else "optional",
        "change_impact": change_impact,
        "change_detection": change_detection,
        "change_summary": change_summary,
    }


def _attach_analysis_context(
    response_payload: dict[str, Any],
    selected_input: str,
    selected_config: str,
    change_detection: dict[str, Any],
    change_summary: str,
    change_impact: dict[str, str],
    change_status: str,
) -> dict[str, Any]:
    if response_payload.get("status") in {"ok", "prepared"}:
        response_payload.update({
            "selected_input": selected_input,
            "selected_config": selected_config,
            "change_detection": change_detection,
            "change_summary": change_summary,
            "change_impact": change_impact,
        })

        if response_payload.get("status") == "prepared":
            response_payload.update({
                "prepared_input_file_name": selected_input,
                "prepared_change_status": change_status,
            })

    return response_payload


def _save_baseline_if_needed(
    response_payload: dict[str, Any],
    input_file_name: str,
    change_status: str,
) -> None:
    if response_payload.get("status") != "ok" or change_status not in {"first_version", "changed"}:
        return

    save_result = save_current_file_as_new_version(input_file_name)
    if save_result.get("status") != "ok":
        response_payload["baseline_save_warning"] = save_result.get("message", "Failed to save new baseline")


def _cleanup_temp_paths(temp_paths: list[Path]) -> None:
    for temp_path in temp_paths:
        if temp_path.exists():
            temp_path.unlink()

def _run_analyze_for_excel_paths(
    input_excel_path: Path,
    config_excel_path: Path,
    input_label: str,
    config_label: str,
    input_sheet_info: str,
    config_sheet_info: str,
    prepare_only: bool = False,
    allow_retry: bool = False,
    max_retries: int = 0,
    backoff_initial_seconds: float = 2.0,
    backoff_multiplier: float = 2.0,
    backoff_max_seconds: float = 30.0,
) -> dict[str, Any]:
    try:
        input_payload = json.loads(extract_columns(str(input_excel_path)))
        config_payload = json.loads(extract_columns(str(config_excel_path)))
    except Exception as error:
        logger.exception("Failed to build extract_columns payloads during analyze request")
        return {"error": f"Failed to extract payloads for analysis: {error}"}

    # Extract actual data rows from the input file so the frontend can build a
    # proper data CSV (column values, not just header positions).
    try:
        input_row_data = json.loads(extract_data_rows(str(input_excel_path)))
    except Exception:
        logger.warning("extract_data_rows failed for %s; input_row_data will be empty", input_label)
        input_row_data = {}

    comparison = compare_payload_columns(input_payload, config_payload)

    logger.info(
        "Analyze payloads ready (input=%s cols, config=%s cols, overlap=%.2f)",
        sum(len(s.get("column_names", [])) for s in input_payload.values() if isinstance(s, dict)),
        sum(len(s.get("column_names", [])) for s in config_payload.values() if isinstance(s, dict)),
        comparison.get("overlap_ratio_vs_config", 0),
    )

    input_payload_str = format_payload_for_llm(input_payload, input_label, "INPUT")
    config_payload_str = format_payload_for_llm(config_payload, config_label, "CONFIG")

    input_sheet_names = [
        key.split("(", 1)[1].rstrip(")").strip()
        for key, value in input_payload.items()
        if isinstance(value, dict) and "(" in key and key.endswith(")")
    ]
    input_sheet_names_line = ", ".join(input_sheet_names) if input_sheet_names else "(none)"
    expected_source_wb_keyword = Path(input_label).stem

    shared = comparison.get("shared_columns", [])
    missing = comparison.get("missing_in_input", [])
    extra = comparison.get("extra_in_input", [])
    overlap_ratio = comparison.get("overlap_ratio_vs_config", 0)
    comparison_summary = (
        "AUTOMATED DETECTION STARTER:\n"
        f"- overlap ratio vs config: {overlap_ratio}\n"
        f"- shared columns ({len(shared)}): {shared[:20]}\n"
        f"- missing in input ({len(missing)}): {missing[:20]}\n"
        f"- extra in input ({len(extra)}): {extra[:20]}"
    )

    prompt = f"""You are an ETL mapping expert. Map INPUT sheets to CONFIG format. Produce one OUTPUT block per INPUT sheet — no extra text.

{input_payload_str}

{config_payload_str}

{comparison_summary}

MAP ONLY THESE SHEETS: {input_sheet_names_line}
source_wb_keyword FOR ALL BLOCKS: {expected_source_wb_keyword}

OUTPUT FORMAT (repeat exactly for every sheet, no numbering, no commentary):
OUTPUT : suggested CONFIG row appendment
source_wb_keyword: {expected_source_wb_keyword}
source_ws: <sheet name from MAP ONLY list — each used exactly once>
item_col_position: <position number>
item_col_name: <column name>
forecast_col_position: <position number>
forecast_col_name: <column name>
data_start_row: <value from INPUT payload, unchanged>

COLUMN SELECTION RULES:
- item column priority: exact match 'Item' > 'ISBN' > 'itm' > 'Widget' > 'Thing'. No match → None/None.
- forecast column: first column containing 'Forecast' or 'FCST'. No match → None/None.
- If a column has duplicates, prefer the one with "marked_with_use_this": true.
- Column positions: use the numeric position number as-is from the INPUT payload. No calculation.
- Unselected columns: append "Skipped: [names]" to that block.
- Do not map CONFIG-only sheets (utility_configs, master_configs, etc.).

Start with the first OUTPUT block. End after the last. No intro, no summary."""

    if prepare_only:
        return {
            "status": "prepared",
            "prompt": prompt,
            "input_file_info": input_sheet_info,
            "config_file_info": config_sheet_info,
            "sheet_names": [],
            "current_sheet_index": 0,
            # input_column_data carries the per-sheet column names + positions from the
            # input file so the frontend can build a synced config CSV without a second request.
            "input_column_data": input_payload,
            # input_row_data carries the actual data rows (headers + values) from the input
            # file so the frontend CSV contains real column data, not just header positions.
            "input_row_data": input_row_data,
            # config_column_data carries the per-sheet column names from the config file so
            # the frontend can produce a CSV that is formatted in the config file's structure.
            "config_column_data": config_payload,
            "detected_issues": {
                "missing_columns": comparison.get("missing_in_input", []),
                "extra_columns": comparison.get("extra_in_input", []),
                "type_mismatches": [],
                "potential_renames": [],
            },
        }

    try:
        llm_start = time.monotonic()
        llm_result = query_llm(
            prompt,
            allow_retry=allow_retry,
            max_retries=max_retries,
            backoff_initial_seconds=backoff_initial_seconds,
            backoff_multiplier=backoff_multiplier,
            backoff_max_seconds=backoff_max_seconds,
        )
        llm_elapsed = round(time.monotonic() - llm_start, 2)
    except Exception as error:
        logger.exception("Unhandled exception while communicating with LLM")
        return {
            "error": f"Failed to communicate with LLM: {error}",
            "error_type": "exception",
            "prompt": prompt,
            "input_file_info": input_sheet_info,
            "config_file_info": config_sheet_info,
        }

    if "error" in llm_result:
        logger.error("Analyze request failed due to LLM error: %s", llm_result["error"])
        return {
            **llm_result,
            "prompt": prompt,
            "input_file_info": input_sheet_info,
            "config_file_info": config_sheet_info,
        }

    logger.info("Analyze request completed successfully")

    return {
        "status": "ok",
        "ai_summary": llm_result.get("response", ""),
        "llm_elapsed_seconds": llm_elapsed,
        "prompt": prompt,
        "input_file_info": input_sheet_info,
        "config_file_info": config_sheet_info,
        "sheet_names": [],
        "current_sheet_index": 0,
        "input_column_data": input_payload,
        "detected_issues": {
            "missing_columns": comparison.get("missing_in_input", []),
            "extra_columns": comparison.get("extra_in_input", []),
            "type_mismatches": [],
            "potential_renames": [],
        },
    }


def _run_prepared_prompt_analysis(
    prompt: str,
    input_sheet_info: str,
    config_sheet_info: str,
    allow_retry: bool = False,
    max_retries: int = 0,
    backoff_initial_seconds: float = 2.0,
    backoff_multiplier: float = 2.0,
    backoff_max_seconds: float = 30.0,
) -> dict[str, Any]:
    try:
        llm_start = time.monotonic()
        llm_result = query_llm(
            prompt,
            allow_retry=allow_retry,
            max_retries=max_retries,
            backoff_initial_seconds=backoff_initial_seconds,
            backoff_multiplier=backoff_multiplier,
            backoff_max_seconds=backoff_max_seconds,
        )
        llm_elapsed = round(time.monotonic() - llm_start, 2)
    except Exception as error:
        logger.exception("Unhandled exception while communicating with LLM")
        return {
            "status": "error",
            "message": f"Failed to communicate with LLM: {error}",
            "error_type": "exception",
            "prompt": prompt,
            "input_file_info": input_sheet_info,
            "config_file_info": config_sheet_info,
        }

    if "error" in llm_result:
        logger.error("Analyze request failed due to LLM error: %s", llm_result["error"])
        return {
            "status": "error",
            "message": llm_result.get("error", "Analyze failed."),
            "prompt": prompt,
            "input_file_info": input_sheet_info,
            "config_file_info": config_sheet_info,
            **llm_result,
        }

    return {
        "status": "ok",
        "ai_summary": llm_result.get("response", ""),
        "llm_elapsed_seconds": llm_elapsed,
        "prompt": prompt,
        "input_file_info": input_sheet_info,
        "config_file_info": config_sheet_info,
        "sheet_names": [],
        "current_sheet_index": 0,
        "detected_issues": {
            "missing_columns": [],
            "extra_columns": [],
            "type_mismatches": [],
            "potential_renames": [],
        },
    }


def _parse_json_or_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return None
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return None
    return None


def _handle_prepared_prompt_request(
    prepared_prompt: str,
    prepared_input_file_info: str | None,
    prepared_config_file_info: str | None,
    prepared_input_file_name: str | None,
    prepared_config_file_name: str | None,
    prepared_input_column_data: dict[str, Any] | None,
    prepared_input_row_data: dict[str, Any] | None,
    prepared_config_column_data: dict[str, Any] | None,
    prepared_change_status: str | None,
    allow_retry: bool,
    max_retries: int,
    backoff_initial_seconds: float,
    backoff_multiplier: float,
    backoff_max_seconds: float,
) -> dict[str, Any]:
    response_payload = _run_prepared_prompt_analysis(
        prepared_prompt,
        prepared_input_file_info or "Prepared input",
        prepared_config_file_info or "Prepared config",
        allow_retry=allow_retry,
        max_retries=max_retries,
        backoff_initial_seconds=backoff_initial_seconds,
        backoff_multiplier=backoff_multiplier,
        backoff_max_seconds=backoff_max_seconds,
    )
    
    # Save combined response record (prepared prompt + LLM response) to data/responses
    if response_payload.get("ai_summary"):
        prepared_data = {
            "prompt": prepared_prompt,
            "input": _build_saved_response_input(
                input_file_info=prepared_input_file_info or "Prepared input",
                config_file_info=prepared_config_file_info or "Prepared config",
                input_file_name=prepared_input_file_name or "",
                change_status=prepared_change_status or "",
                config_file_name=prepared_config_file_name,
            ),
        }
        if prepared_input_column_data:
            prepared_data["input"]["input_column_data"] = prepared_input_column_data
        if prepared_input_row_data:
            prepared_data["input"]["input_row_data"] = prepared_input_row_data
        if prepared_config_column_data:
            prepared_data["input"]["config_column_data"] = prepared_config_column_data
        combined_result = save_combined_response_record(
            prepared_data,
            response_payload.get("ai_summary", ""),
        )
        response_payload["combined_response_record"] = combined_result

    if prepared_input_file_name:
        _save_baseline_if_needed(response_payload, prepared_input_file_name, str(prepared_change_status))
    return response_payload


# ── Streaming helpers ────────────────────────────────────────────────────────

def _stream_json_line(payload: dict[str, Any]) -> bytes:
    """Encode a dict as a single NDJSON line."""
    return (json.dumps(payload) + "\n").encode("utf-8")


def _stream_prepared_prompt_analysis(
    prompt: str,
    input_sheet_info: str,
    config_sheet_info: str,
):
    """Yield NDJSON bytes for a streaming Ollama analysis run."""
    full_response_parts: list[str] = []
    started = time.monotonic()

    yield _stream_json_line({"event": "start"})

    for event in query_llm_stream(prompt):
        event_type = event.get("type")

        if event_type == "chunk":
            chunk = str(event.get("content", ""))
            if chunk:
                full_response_parts.append(chunk)
                yield _stream_json_line({"event": "chunk", "content": chunk})
            continue

        if event_type == "error":
            logger.error("Streamed analysis failed: %s", event.get("error"))
            yield _stream_json_line({
                "event": "error",
                "payload": {
                    "status": "error",
                    "error": event.get("error", "Analysis failed."),
                    "error_type": event.get("error_type", "unknown"),
                    "retryable": event.get("retryable", False),
                    "attempts": event.get("attempts", 1),
                    "input_file_info": input_sheet_info,
                    "config_file_info": config_sheet_info,
                },
            })
            return

        if event_type == "done":
            elapsed = round(time.monotonic() - started, 2)
            full_response = "".join(full_response_parts)
            logger.info("Streaming analysis completed in %ss", elapsed)
            yield _stream_json_line({
                "event": "complete",
                "payload": {
                    "status": "ok",
                    "ai_summary": full_response,
                    "llm_elapsed_seconds": elapsed,
                    "input_file_info": input_sheet_info,
                    "config_file_info": config_sheet_info,
                },
            })
            return

    yield _stream_json_line({
        "event": "error",
        "payload": {
            "status": "error",
            "error": "Stream ended without a completion event.",
            "input_file_info": input_sheet_info,
            "config_file_info": config_sheet_info,
        },
    })


@app.post("/analyze/execute-stream")
def analyze_execute_stream(data: dict = Body(default={})) -> StreamingResponse:
    """
    Streaming analysis endpoint.  Accepts the prepared prompt and file metadata,
    streams Ollama output as NDJSON chunks, saves the combined response record,
    and removes temporary prepared data.
    """
    prepared_prompt = str(data.get("prepared_prompt") or "")
    prepared_input_file_info = str(data.get("prepared_input_file_info") or "Prepared input")
    prepared_config_file_info = str(data.get("prepared_config_file_info") or "Prepared config")
    prepared_input_file_name = data.get("prepared_input_file_name")
    prepared_config_file_name = data.get("prepared_config_file_name")
    prepared_input_column_data = _parse_json_or_dict(data.get("prepared_input_column_data"))
    prepared_input_row_data = _parse_json_or_dict(data.get("prepared_input_row_data"))
    prepared_config_column_data = _parse_json_or_dict(data.get("prepared_config_column_data"))
    prepared_change_status = data.get("prepared_change_status")

    def event_stream():
        final_payload: dict[str, Any] | None = None
        for chunk in _stream_prepared_prompt_analysis(
            prepared_prompt,
            prepared_input_file_info,
            prepared_config_file_info,
        ):
            try:
                parsed = json.loads(chunk.decode("utf-8"))
                if parsed.get("event") == "complete":
                    final_payload = parsed.get("payload")
            except Exception:
                pass
            yield chunk

        if final_payload and final_payload.get("ai_summary"):
            # Save combined response record (prepared prompt + LLM response) to data/responses
            prepared_data = {
                "prompt": prepared_prompt,
                "input": _build_saved_response_input(
                    input_file_info=prepared_input_file_info,
                    config_file_info=prepared_config_file_info,
                    input_file_name=prepared_input_file_name or "",
                    change_status=prepared_change_status or "",
                    config_file_name=prepared_config_file_name,
                ),
            }
            if prepared_input_column_data:
                prepared_data["input"]["input_column_data"] = prepared_input_column_data
            if prepared_input_row_data:
                prepared_data["input"]["input_row_data"] = prepared_input_row_data
            if prepared_config_column_data:
                prepared_data["input"]["config_column_data"] = prepared_config_column_data
            combined_result = save_combined_response_record(
                prepared_data,
                final_payload.get("ai_summary", ""),
            )
            final_payload["combined_response_record"] = combined_result

            if prepared_input_file_name:
                _save_baseline_if_needed(
                    final_payload,
                    str(prepared_input_file_name),
                    str(prepared_change_status),
                )

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/analyze-excel_files-from-input")
def analyze_excel_files_from_input(data: dict = Body(default={})) -> dict[str, Any]:
    prepared_prompt = data.get("prepared_prompt")
    if prepared_prompt:
        return _handle_prepared_prompt_request(
            str(prepared_prompt),
            data.get("prepared_input_file_info"),
            data.get("prepared_config_file_info"),
            data.get("prepared_input_file_name"),
            data.get("prepared_config_file_name"),
            _parse_json_or_dict(data.get("prepared_input_column_data")),
            _parse_json_or_dict(data.get("prepared_input_row_data")),
            _parse_json_or_dict(data.get("prepared_config_column_data")),
            data.get("prepared_change_status"),
            allow_retry=bool(data.get("allow_retry", False)),
            max_retries=int(data.get("max_retries", 0) or 0),
            backoff_initial_seconds=float(data.get("backoff_initial_seconds", 2.0) or 2.0),
            backoff_multiplier=float(data.get("backoff_multiplier", 2.0) or 2.0),
            backoff_max_seconds=float(data.get("backoff_max_seconds", 30.0) or 30.0),
        )

    input_candidates = _list_input_candidates()
    if not input_candidates:
        return {
            "status": "needs_input_upload",
            "message": "No input files detected in input folder. Upload files first.",
        }

    config_candidates = _list_config_candidates()

    requested_input = data.get("input_file_name")
    requested_config = data.get("config_file_name")
    ignore_saved_default_config = bool(data.get("ignore_saved_default_config", False))
    prepare_only = bool(data.get("prepare_only", False))
    save_default_config = bool(data.get("save_default_config"))
    confirm_ai_analysis = bool(data.get("confirm_ai_analysis", False))

    input_path = _resolve_by_name(input_candidates, requested_input) if requested_input else _select_default_input(input_candidates)
    if not input_path:
        return {
            "status": "needs_input_upload",
            "message": "No usable input file found.",
        }

    config_path = _resolve_by_name(config_candidates, requested_config)
    if not config_path and not ignore_saved_default_config:
        default_config_name = _read_default_config_name()
        config_path = _resolve_by_name(config_candidates, default_config_name)

    if not config_path and len(config_candidates) == 1:
        config_path = config_candidates[0]

    if not config_path and len(config_candidates) > 1:
        return {
            "status": "needs_config_selection",
            "message": "Multiple config files found in data/config. Choose one for this run. Recommendation: remove extras from data/config.",
            "selected_input": input_path.name,
            "config_options": [path.name for path in config_candidates],
            "default_config": _read_default_config_name(),
            "config_dir": str(_config_dir()),
        }

    if not config_path:
        return {
            "status": "needs_config_upload",
            "message": "No config file found in data/config. Choose one from your files and it will be uploaded to data/config and saved there.",
            "selected_input": input_path.name,
            "config_dir": str(_config_dir()),
        }

    if save_default_config:
        _save_default_config_name(config_path.name)

    change_detection = compare_file_without_updating(input_path.name)
    if change_detection.get("status") == "error":
        return {
            "status": "error",
            "message": change_detection.get("message", "Failed to detect file changes."),
            "selected_input": input_path.name,
            "selected_config": config_path.name,
        }

    change_status = change_detection.get("status")
    change_summary = _summarize_change_detection(change_detection)
    change_impact = _classify_change_impact(change_detection)

    if change_status in {"changed", "no_change"} and not confirm_ai_analysis:
        return _build_analysis_confirmation_response(
            change_status,
            change_summary,
            input_path.name,
            config_path.name,
            change_impact,
            change_detection,
        )

    temp_paths: list[Path] = []
    try:
        input_excel_path, input_sheet_info = _materialize_existing_as_excel(input_path, temp_paths)
        config_excel_path, config_sheet_info = _materialize_existing_as_excel(config_path, temp_paths)
        result = _run_analyze_for_excel_paths(
            input_excel_path,
            config_excel_path,
            input_path.name,
            config_path.name,
            input_sheet_info,
            config_sheet_info,
            prepare_only=prepare_only,
            allow_retry=bool(data.get("allow_retry", False)),
            max_retries=int(data.get("max_retries", 0) or 0),
            backoff_initial_seconds=float(data.get("backoff_initial_seconds", 2.0) or 2.0),
            backoff_multiplier=float(data.get("backoff_multiplier", 2.0) or 2.0),
            backoff_max_seconds=float(data.get("backoff_max_seconds", 30.0) or 30.0),
        )
        if "error" in result and "status" not in result:
            return {
                "status": "error",
                "message": result.get("error", "Analyze failed."),
                "selected_input": input_path.name,
                "selected_config": config_path.name,
                **result,
            }
        result = _attach_analysis_context(
            result,
            input_path.name,
            config_path.name,
            change_detection,
            change_summary,
            change_impact,
            change_status,
        )
        _save_baseline_if_needed(result, input_path.name, change_status)
        return result
    finally:
        _cleanup_temp_paths(temp_paths)


# API endpoint to query the LLM to read Excel files and detect inconsistencies.
# Supports a two-phase flow used by the frontend:
#   Phase 1 (prepare): input_file + config_file uploaded; returns prompt data for review.
#   Phase 2 (execute): prepared_prompt sent as form field; calls LLM and returns ai_summary.
@app.post("/analyze-excel_files")
async def analyze_excel_files(
    input_file: Optional[UploadFile] = File(default=None),
    config_file: Optional[UploadFile] = File(default=None),
    prepare_only: str = Form(default="false"),
    confirm_ai_analysis: str = Form(default="false"),
    prepared_prompt: Optional[str] = Form(default=None),
    prepared_input_file_info: Optional[str] = Form(default=None),
    prepared_config_file_info: Optional[str] = Form(default=None),
    prepared_input_file_name: Optional[str] = Form(default=None),
    prepared_config_file_name: Optional[str] = Form(default=None),
    prepared_input_column_data: Optional[str] = Form(default=None),
    prepared_input_row_data: Optional[str] = Form(default=None),
    prepared_config_column_data: Optional[str] = Form(default=None),
    prepared_change_status: Optional[str] = Form(default=None),
    allow_retry: str = Form(default="false"),
    max_retries: str = Form(default="0"),
    backoff_initial_seconds: str = Form(default="3"),
    backoff_multiplier: str = Form(default="2"),
    backoff_max_seconds: str = Form(default="30"),
):
    # ── Phase 2: execute with a pre-built prompt (no file uploads) ──────────
    if prepared_prompt:
        return _handle_prepared_prompt_request(
            prepared_prompt,
            prepared_input_file_info,
            prepared_config_file_info,
            prepared_input_file_name,
            prepared_config_file_name,
            _parse_json_or_dict(prepared_input_column_data),
            _parse_json_or_dict(prepared_input_row_data),
            _parse_json_or_dict(prepared_config_column_data),
            prepared_change_status,
            allow_retry=allow_retry,
            max_retries=max_retries,
            backoff_initial_seconds=backoff_initial_seconds,
            backoff_multiplier=backoff_multiplier,
            backoff_max_seconds=backoff_max_seconds,
        )

    if input_file is None or config_file is None:
        return {
            "error": "Both input_file and config_file are required unless prepared_prompt is provided.",
            "error_type": "missing_files",
        }

    logger.info(
        "Analyze prepare phase started (input_file=%s, config_file=%s)",
        input_file.filename,
        config_file.filename,
    )

    input_name = input_file.filename or "input_file"
    config_name = config_file.filename or "config_file"

    try:
        input_raw_bytes = await input_file.read()
    except Exception as e:
        return {"error": f"Failed to read input file: {str(e)}"}

    try:
        config_raw_bytes = await config_file.read()
    except Exception as e:
        return {"error": f"Failed to read config file: {str(e)}"}

    input_dir = Path(default_input_dir())
    input_dir.mkdir(parents=True, exist_ok=True)
    staged_input_path = input_dir / input_name
    staged_input_path.write_bytes(input_raw_bytes)

    change_detection = compare_file_without_updating(input_name)
    if change_detection.get("status") == "error":
        return {
            "status": "error",
            "message": change_detection.get("message", "Failed to detect file changes."),
            "selected_input": input_name,
            "selected_config": config_name,
        }

    change_status = change_detection.get("status")
    change_summary = _summarize_change_detection(change_detection)
    change_impact = _classify_change_impact(change_detection)

    if change_status in {"changed", "no_change"} and not confirm_ai_analysis:
        return _build_analysis_confirmation_response(
            change_status,
            change_summary,
            input_name,
            config_name,
            change_impact,
            change_detection,
        )

    temp_paths: list[Path] = []

    try:
        input_excel_path, input_sheet_info = _materialize_uploaded_bytes_as_excel(input_name, input_raw_bytes, "input", temp_paths)
    except Exception as e:
        logger.exception("Failed to prepare input file during analyze request")
        _cleanup_temp_paths(temp_paths)
        return {"error": f"Failed to prepare input file: {str(e)}"}

    try:
        config_excel_path, config_sheet_info = _materialize_uploaded_bytes_as_excel(config_name, config_raw_bytes, "config", temp_paths)
    except Exception as e:
        logger.exception("Failed to prepare config file during analyze request")
        _cleanup_temp_paths(temp_paths)
        return {"error": f"Failed to prepare config file: {str(e)}"}

    try:
        response_payload = _run_analyze_for_excel_paths(
            input_excel_path,
            config_excel_path,
            input_name,
            config_name,
            input_sheet_info,
            config_sheet_info,
            prepare_only=prepare_only,
            allow_retry=allow_retry,
            max_retries=max_retries,
            backoff_initial_seconds=backoff_initial_seconds,
            backoff_multiplier=backoff_multiplier,
            backoff_max_seconds=backoff_max_seconds,
        )
    finally:
        _cleanup_temp_paths(temp_paths)

    if "error" in response_payload and "status" not in response_payload:
        return {
            "status": "error",
            "message": response_payload.get("error", "Analyze failed."),
            "selected_input": input_name,
            "selected_config": config_name,
            **response_payload,
        }

    response_payload = _attach_analysis_context(
        response_payload,
        input_name,
        config_name,
        change_detection,
        change_summary,
        change_impact,
        change_status,
    )
    _save_baseline_if_needed(response_payload, input_name, change_status)

    return response_payload
# ===== DEBUG FILE LIST =====

import os

def _debug_list_files(path: str):
    if not os.path.exists(path):
        return []

    return sorted(
        [
            name for name in os.listdir(path)
            if os.path.isfile(os.path.join(path, name))
        ]
    )

@app.get("/debug/files")
def debug_files():
    # Use SAME directories as the rest of your app
    input_path = default_input_dir()
    config_path = str(_config_dir())

    return {
        "input_path": os.path.abspath(input_path),
        "config_path": os.path.abspath(config_path),
        "input_files": _debug_list_files(input_path),
        "config_files": _debug_list_files(config_path),
    }