import os


# Prints an informational status message with [Info] prefix.
def status(msg: str) -> None:
    print(f"[Info] {msg}")


# Prints a warning message with [Warning] prefix.
def warn(msg: str) -> None:
    print(f"[Warning] {msg}")


# Prints an error message with [Error] prefix.
def error(msg: str) -> None:
    print(f"[Error] {msg}")


# Returns the input directory path from environment variable or default.
def default_input_dir() -> str:
    return os.environ.get("INPUT_DIR", "./data/input")


# Returns the output directory path from environment variable or default.
def default_output_dir() -> str:
    return os.environ.get("OUTPUT_DIR", "./data/output")


# Returns the hash directory path from environment variable or default.
def default_hash_dir() -> str:
    return os.environ.get("HASH_DIR", os.path.join(default_state_dir(), "hashes"))


# Returns the state directory path for manifests, caches, and metadata files.
def default_state_dir() -> str:
    return os.environ.get("STATE_DIR", "./data/state")


# Returns the changed files manifest path (stored in state root, not in hash dir).
def default_changed_files_path() -> str:
    return os.path.join(default_state_dir(), ".changed_files.json")


# Returns the archive directory path used for saved baselines and history snapshots.
def default_archive_dir() -> str:
    return os.environ.get("ARCHIVE_DIR", "./data/archive")


# Creates a data folder if it doesn't exist and prints status messages.
def ensure_data_folder(folder_path: str) -> None:
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        warn(f"Data folder '{folder_path}' did not exist. Created it.")
    else:
        status(f"Data folder '{folder_path}' found.")
