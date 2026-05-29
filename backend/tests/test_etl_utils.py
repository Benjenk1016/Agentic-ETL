from pathlib import Path

import pytest

from backend.src import etl_utils


# Tests that default directory functions return custom values from environment variables.
# Calls etl_utils.default_input_dir, default_output_dir, and default_hash_dir.
def test_default_dirs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INPUT_DIR", "custom/input")
    monkeypatch.setenv("OUTPUT_DIR", "custom/output")
    monkeypatch.setenv("HASH_DIR", "custom/hashes")

    assert etl_utils.default_input_dir() == "custom/input"
    assert etl_utils.default_output_dir() == "custom/output"
    assert etl_utils.default_hash_dir() == "custom/hashes"


# Tests that default directory functions fall back to default values when env vars are unset.
# Calls etl_utils.default_input_dir, default_output_dir, and default_hash_dir.
def test_default_dirs_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INPUT_DIR", raising=False)
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    monkeypatch.delenv("HASH_DIR", raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("ARCHIVE_DIR", raising=False)

    assert etl_utils.default_input_dir() == "./data/input"
    assert etl_utils.default_output_dir() == "./data/output"
    assert etl_utils.default_hash_dir() == "./data/state/hashes"
    assert etl_utils.default_state_dir() == "./data/state"
    assert etl_utils.default_archive_dir() == "./data/archive"


# Tests that ensure_data_folder creates a directory when it doesn't exist.
# Calls etl_utils.ensure_data_folder.
def test_ensure_data_folder_creates(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assert not data_dir.exists()

    etl_utils.ensure_data_folder(str(data_dir))
    assert data_dir.exists()
