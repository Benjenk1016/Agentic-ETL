import os
import shutil
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import pandas as pd

from backend.src.etl_utils import default_archive_dir, default_hash_dir, default_input_dir


def _baseline_history_mode() -> str:
    mode = os.environ.get("BASELINE_HISTORY_MODE", "all").strip().lower()
    if mode in {"all", "latest"}:
        return mode
    return "all"


# this loads a file as a dict of sheets to DataFrames
def load_file_sheets(path):
    if path.lower().endswith(".csv"):
        return {
            "CSV": pd.read_csv(
                path,
                dtype=str,
                keep_default_na=False,
            )
        }
    return pd.read_excel(
        path,
        sheet_name=None,
        dtype=str,
        keep_default_na=False,
    )


def _empty_change_result(file_name, status, message, changed):
    return {
        "file": file_name,
        "status": status,
        "message": message,
        "changed": changed,
        "columns_added": [],
        "columns_removed": [],
        "row_count_old": 0,
        "row_count_new": 0,
        "value_changes": [],
        "added_rows": [],
        "removed_rows": [],
        "updated_rows": [],
        "sheet_changes": {
            "added_sheets": [],
            "removed_sheets": [],
            "updated_sheets": [],
        },
        "sheet_details": {},
    }


def _normalize_cell_value(value):
    if pd.isna(value):
        return ""

    # Keep bool distinct from ints (True should not equal 1).
    if isinstance(value, bool):
        return f"bool:{value}"

    if isinstance(value, (int, float)):
        if isinstance(value, float) and not pd.notna(value):
            return ""
        return str(Decimal(str(value)).normalize())

    text = str(value).strip()
    if not text:
        return ""

    try:
        return str(Decimal(text).normalize())
    except (InvalidOperation, ValueError):
        return text


def _compare_dataframes(old_df, new_df):
    old_cols = list(old_df.columns)
    new_cols = list(new_df.columns)

    columns_added = [col for col in new_cols if col not in old_cols]
    columns_removed = [col for col in old_cols if col not in new_cols]

    value_changes = []
    updated_rows = set()
    rows = min(len(old_df), len(new_df))

    for i in range(rows):
        row_changed = False
        for col in old_cols:
            if col in new_cols:
                old_val = old_df.iloc[i][col]
                new_val = new_df.iloc[i][col]

                if pd.isna(old_val):
                    old_val = ""
                if pd.isna(new_val):
                    new_val = ""

                if _normalize_cell_value(old_val) != _normalize_cell_value(new_val):
                    row_changed = True
                    value_changes.append({
                        "row": i + 1,
                        "column": col,
                        "old": str(old_val),
                        "new": str(new_val)
                    })

        if row_changed:
            updated_rows.add(i + 1)

    added_rows = list(range(len(old_df) + 1, len(new_df) + 1)) if len(new_df) > len(old_df) else []
    removed_rows = list(range(len(new_df) + 1, len(old_df) + 1)) if len(old_df) > len(new_df) else []

    return {
        "columns_added": columns_added,
        "columns_removed": columns_removed,
        "row_count_old": len(old_df),
        "row_count_new": len(new_df),
        "value_changes": value_changes,
        "added_rows": added_rows,
        "removed_rows": removed_rows,
        "updated_rows": sorted(updated_rows),
    }


def _compare_saved_vs_input(file_path_or_name):
    # Accept either a full path or just a file name
    if os.path.isabs(file_path_or_name) or os.path.exists(file_path_or_name):
        input_path = file_path_or_name
        file_name = os.path.basename(file_path_or_name)
    else:
        input_dir = default_input_dir()
        input_path = os.path.join(input_dir, file_path_or_name)
        file_name = file_path_or_name
    saved_dir = get_saved_dir()
    saved_path = os.path.join(saved_dir, file_name)

    if not os.path.exists(input_path):
        return {
            "file": file_name,
            "status": "error",
            "message": f"File not found in input folder: {input_path}"
        }

    new_hash = get_file_hash(input_path)
    old_hash = read_old_hash(file_name)

    if not os.path.exists(saved_path) or old_hash is None:
        return _empty_change_result(
            file_name=file_name,
            status="first_version",
            message="No older saved version exists yet",
            changed=True,
        )

    if new_hash == old_hash:
        return _empty_change_result(
            file_name=file_name,
            status="no_change",
            message="No changes found",
            changed=False,
        )

    old_sheets = load_file_sheets(saved_path)
    new_sheets = load_file_sheets(input_path)

    old_sheet_names = set(old_sheets.keys())
    new_sheet_names = set(new_sheets.keys())

    added_sheets = sorted(new_sheet_names - old_sheet_names)
    removed_sheets = sorted(old_sheet_names - new_sheet_names)
    shared_sheets = sorted(old_sheet_names & new_sheet_names)

    sheet_details = {}
    aggregate_columns_added = []
    aggregate_columns_removed = []
    aggregate_value_changes = []
    aggregate_added_rows = []
    aggregate_removed_rows = []
    aggregate_updated_rows = []

    updated_sheets = []
    for sheet_name in shared_sheets:
        details = _compare_dataframes(old_sheets[sheet_name], new_sheets[sheet_name])
        sheet_details[sheet_name] = details

        sheet_changed = bool(
            details["columns_added"]
            or details["columns_removed"]
            or details["value_changes"]
            or details["added_rows"]
            or details["removed_rows"]
        )
        if sheet_changed:
            updated_sheets.append(sheet_name)

        aggregate_columns_added.extend([f"{sheet_name}.{col}" for col in details["columns_added"]])
        aggregate_columns_removed.extend([f"{sheet_name}.{col}" for col in details["columns_removed"]])
        aggregate_value_changes.extend([
            {"sheet": sheet_name, **change} for change in details["value_changes"]
        ])
        aggregate_added_rows.extend([f"{sheet_name}:{row}" for row in details["added_rows"]])
        aggregate_removed_rows.extend([f"{sheet_name}:{row}" for row in details["removed_rows"]])
        aggregate_updated_rows.extend([f"{sheet_name}:{row}" for row in details["updated_rows"]])

    has_changes = bool(added_sheets or removed_sheets or updated_sheets)

    return {
        "file": file_name,
        "status": "changed" if has_changes else "no_change",
        "message": "Changes found" if has_changes else "No changes found",
        "changed": has_changes,
        "columns_added": aggregate_columns_added,
        "columns_removed": aggregate_columns_removed,
        "row_count_old": sum(len(df) for df in old_sheets.values()),
        "row_count_new": sum(len(df) for df in new_sheets.values()),
        "value_changes": aggregate_value_changes,
        "added_rows": aggregate_added_rows,
        "removed_rows": aggregate_removed_rows,
        "updated_rows": aggregate_updated_rows,
        "sheet_changes": {
            "added_sheets": added_sheets,
            "removed_sheets": removed_sheets,
            "updated_sheets": updated_sheets,
        },
        "sheet_details": sheet_details,
    }


# this creates a hash so we can see if the file changed
def get_file_hash(path):
    hasher = hashlib.md5()

    with open(path, "rb") as file:
        while True:
            chunk = file.read(4096)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()


# this gets the folder where we save the previous versions of files
def get_saved_dir():
    saved_dir = default_archive_dir()

    # make folder if it doesn't exist yet
    os.makedirs(saved_dir, exist_ok=True)

    return saved_dir


def _archived_baseline_paths(file_name, saved_dir=None):
    base_dir = Path(saved_dir or get_saved_dir())
    stem, ext = os.path.splitext(file_name)
    archived_paths = [path for path in base_dir.glob(f"{stem}__archived_*{ext}") if path.is_file()]
    return sorted(archived_paths, key=lambda path: (path.stat().st_mtime, path.name))


def _resolve_saved_baseline_path(file_name, saved_dir=None):
    base_dir = Path(saved_dir or get_saved_dir())
    saved_path = base_dir / file_name
    if saved_path.exists():
        return saved_path

    archived_paths = _archived_baseline_paths(file_name, str(base_dir))
    if archived_paths:
        return archived_paths[-1]

    return None


def _remove_archived_baselines(file_name, saved_dir=None):
    for archived_path in _archived_baseline_paths(file_name, saved_dir):
        try:
            archived_path.unlink()
        except FileNotFoundError:
            continue


# this finds where the hash file should be stored
def get_hash_file_path(file_name, hash_dir=None):
    if hash_dir is None:
        hash_dir = default_hash_dir()
    # make folder if it doesn't exist
    os.makedirs(hash_dir, exist_ok=True)
    return os.path.join(hash_dir, file_name + ".hash")


# read the old hash so we can compare it
def read_old_hash(file_name, hash_dir=None):
    hash_path = get_hash_file_path(file_name, hash_dir)

    if not os.path.exists(hash_path):
        return None

    with open(hash_path, "r") as file:
        return file.read().strip()


# save the new hash after we process the file
def save_new_hash(file_name, new_hash, hash_dir=None):
    hash_path = get_hash_file_path(file_name, hash_dir)

    with open(hash_path, "w") as file:
        file.write(new_hash)


# main function that checks if the file changed
def compare_file(file_name):

    result = _compare_saved_vs_input(file_name)
    if result.get("status") in {"first_version", "changed"}:
        save_result = save_current_file_as_new_version(file_name)
        if save_result.get("status") != "ok":
            return {
                "file": file_name,
                "status": "error",
                "message": save_result.get("message", "Failed to save new baseline")
            }
    return result

# compare the file but do NOT overwrite the saved version yet
def compare_file_without_updating(file_path_or_name):
    return _compare_saved_vs_input(file_path_or_name)


# save the current uploaded file as the new baseline after comparison
def save_current_file_as_new_version(file_name):
    input_dir = default_input_dir()
    saved_dir = get_saved_dir()

    input_path = os.path.join(input_dir, file_name)
    saved_path = os.path.join(saved_dir, file_name)

    if not os.path.exists(input_path):
        return {
            "status": "error",
            "message": f"File not found in input folder: {input_path}"
        }

    new_hash = get_file_hash(input_path)

    # Keep or prune historical snapshots depending on retention mode.
    if os.path.exists(saved_path) and _baseline_history_mode() == "all":
        stem, ext = os.path.splitext(file_name)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archived_name = f"{stem}__archived_{timestamp}{ext}"
        archived_path = os.path.join(saved_dir, archived_name)
        counter = 1
        while os.path.exists(archived_path):
            archived_name = f"{stem}__archived_{timestamp}_{counter}{ext}"
            archived_path = os.path.join(saved_dir, archived_name)
            counter += 1
        shutil.move(saved_path, archived_path)
    elif _baseline_history_mode() == "latest":
        _remove_archived_baselines(file_name, saved_dir)

    shutil.copy2(input_path, saved_path)
    save_new_hash(file_name, new_hash)

    return {
        "status": "ok",
        "message": "Saved new version"
    }