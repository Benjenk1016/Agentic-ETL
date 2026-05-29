from datetime import datetime, timezone
import hashlib
from pathlib import Path

import msal
import pytest
import requests

import backend.src.onedrive_download as onedrive_download


# Tests that sanitize_filename replaces forbidden Windows characters.
# Calls onedrive_download.sanitize_filename.
def test_sanitize_filename_removes_invalid_chars() -> None:
    assert onedrive_download.sanitize_filename('file<>name.txt') == 'file__name.txt'
    assert onedrive_download.sanitize_filename('test:file|name.xlsx') == 'test_file_name.xlsx'
    assert onedrive_download.sanitize_filename('test"file?.py') == 'test_file_.py'
    assert onedrive_download.sanitize_filename('file\\*name/test.csv') == 'file__name_test.csv'


# Tests that sanitize_filename preserves valid characters.
# Calls onedrive_download.sanitize_filename.
def test_sanitize_filename_preserves_valid() -> None:
    assert onedrive_download.sanitize_filename('valid-file_name.123.txt') == 'valid-file_name.123.txt'
    assert onedrive_download.sanitize_filename('CamelCaseName.XLSX') == 'CamelCaseName.XLSX'


# Tests that utc_now_iso returns properly formatted ISO 8601 string with Z suffix.
# Calls onedrive_download.utc_now_iso.
def test_utc_now_iso_format() -> None:
    result = onedrive_download.utc_now_iso()
    assert isinstance(result, str)
    assert result.endswith('Z')
    # Verify it's parseable as ISO format
    dt = datetime.fromisoformat(result.replace('Z', '+00:00'))
    assert dt.tzinfo is not None


# Tests that load_manifest creates a DataFrame with expected columns when file doesn't exist.
# Calls onedrive_download.load_manifest.
def test_load_manifest_creates_empty(tmp_path: Path) -> None:
    manifest_path = tmp_path / 'manifest.xlsx'
    df = onedrive_download.load_manifest(manifest_path)
    
    assert not df.empty or len(df) == 0
    for col in onedrive_download.MANIFEST_COLUMNS:
        assert col in df.columns


# Tests that load_manifest reads back previously saved data.
# Calls onedrive_download.load_manifest and onedrive_download.save_manifest.
def test_load_manifest_reads_existing(tmp_path: Path) -> None:
    manifest_path = tmp_path / 'manifest.xlsx'
    
    # Create and save a manifest with test data
    row = {
        'item_id': 'test_id_123',
        'name': 'TestFile.xlsx',
        'remote_path': '/Folder/TestFile.xlsx',
        'local_path': '/data/input/TestFile.xlsx',
        'etag': 'etag_value',
        'last_modified': '2026-02-16T00:00:00Z',
        'size': 5000,
        'sha256': 'abc123',
        'downloaded_at': '2026-02-16T05:00:00Z',
        'last_checked_at': '2026-02-16T05:00:00Z',
    }
    df = onedrive_download.upsert_manifest_row(
        onedrive_download.load_manifest(manifest_path), row
    )
    onedrive_download.save_manifest(df, manifest_path)
    
    # Load it back
    loaded = onedrive_download.load_manifest(manifest_path)
    assert 'test_id_123' in loaded['item_id'].values
    assert 'TestFile.xlsx' in loaded['name'].values


# Tests that save_manifest creates directory if it doesn't exist.
# Calls onedrive_download.save_manifest.
def test_save_manifest_creates_directory(tmp_path: Path) -> None:
    nested_path = tmp_path / 'deep' / 'nested' / 'manifest.xlsx'
    assert not nested_path.parent.exists()
    
    df = onedrive_download.load_manifest(nested_path)
    onedrive_download.save_manifest(df, nested_path)
    
    assert nested_path.exists()
    assert nested_path.parent.exists()


# Tests that upsert_manifest_row creates new row in empty DataFrame.
# Calls onedrive_download.upsert_manifest_row.
def test_upsert_manifest_row_insert_empty() -> None:
    df = onedrive_download.load_manifest(Path('nonexistent'))
    row = {
        'item_id': 'id_001',
        'name': 'file.xlsx',
        'remote_path': '/file.xlsx',
        'local_path': '/data/file.xlsx',
        'etag': 'etag',
        'last_modified': '2026-02-16T00:00:00Z',
        'size': 1000,
        'sha256': 'hash',
        'downloaded_at': '2026-02-16T00:00:00Z',
        'last_checked_at': '2026-02-16T00:00:00Z',
    }
    
    result = onedrive_download.upsert_manifest_row(df, row)
    assert len(result) == 1
    assert result.iloc[0]['item_id'] == 'id_001'


# Tests that upsert_manifest_row updates existing row with same item_id.
# Calls onedrive_download.upsert_manifest_row.
def test_upsert_manifest_row_update_existing() -> None:
    df = onedrive_download.load_manifest(Path('nonexistent'))
    row1 = {
        'item_id': 'id_001',
        'name': 'file.xlsx',
        'remote_path': '/file.xlsx',
        'local_path': '/data/file.xlsx',
        'etag': 'etag_old',
        'last_modified': '2026-02-16T00:00:00Z',
        'size': 1000,
        'sha256': 'hash_old',
        'downloaded_at': '2026-02-16T00:00:00Z',
        'last_checked_at': '2026-02-16T00:00:00Z',
    }
    df = onedrive_download.upsert_manifest_row(df, row1)
    
    row2 = {
        'item_id': 'id_001',
        'name': 'file.xlsx',
        'remote_path': '/file.xlsx',
        'local_path': '/data/file.xlsx',
        'etag': 'etag_new',
        'last_modified': '2026-02-17T00:00:00Z',
        'size': 2000,
        'sha256': 'hash_new',
        'downloaded_at': '2026-02-17T00:00:00Z',
        'last_checked_at': '2026-02-17T00:00:00Z',
    }
    df = onedrive_download.upsert_manifest_row(df, row2)
    
    assert len(df) == 1
    assert df.iloc[0]['etag'] == 'etag_new'
    assert df.iloc[0]['sha256'] == 'hash_new'


# Tests that load_token_cache returns empty cache when file doesn't exist.
# Calls onedrive_download.load_token_cache.
def test_load_token_cache_missing() -> None:
    cache = onedrive_download.load_token_cache(Path('nonexistent/token.bin'))
    assert isinstance(cache, msal.SerializableTokenCache)


# Tests that save_token_cache creates file with serialized cache.
# Calls onedrive_download.save_token_cache.
def test_save_token_cache_creates_file(tmp_path: Path) -> None:
    cache_path = tmp_path / 'token.bin'
    cache = msal.SerializableTokenCache()
    # Note: cache needs state change to be saved
    cache.has_state_changed = True
    
    onedrive_download.save_token_cache(cache, cache_path)
    
    assert cache_path.exists()


# Tests that resolve_local_path returns existing path if provided.
# Calls onedrive_download.resolve_local_path.
def test_resolve_local_path_returns_existing() -> None:
    existing_path = '/data/input/existing_file.xlsx'
    result = onedrive_download.resolve_local_path(
        Path('/data/input'),
        'file.xlsx',
        'item_123',
        existing_path
    )
    assert str(result) == existing_path


# Tests that resolve_local_path creates safe name and avoids collisions.
# Calls onedrive_download.resolve_local_path.
def test_resolve_local_path_sanitizes_name(tmp_path: Path) -> None:
    result = onedrive_download.resolve_local_path(
        tmp_path,
        'file<invalid>name.xlsx',
        'item_123',
        None
    )
    assert 'file_invalid_name.xlsx' in str(result)


# Tests that resolve_local_path appends item_id on collision.
# Calls onedrive_download.resolve_local_path.
def test_resolve_local_path_handles_collision(tmp_path: Path) -> None:
    # Create existing file
    existing = tmp_path / 'file.xlsx'
    existing.write_text('existing')
    
    # Request same filename should add item_id suffix (first 8 chars)
    result = onedrive_download.resolve_local_path(
        tmp_path,
        'file.xlsx',
        'item_abc1234',
        None
    )
    assert 'file__item_abc' in str(result)


# Tests that build_remote_path correctly constructs path from item data.
# Calls onedrive_download.build_remote_path.
def test_build_remote_path_simple() -> None:
    item = {
        'name': 'TestFile.xlsx',
        'parentReference': {'path': '/drive/root:/Folder'}
    }
    result = onedrive_download.build_remote_path(item)
    assert 'Folder' in result
    assert 'TestFile.xlsx' in result


# Tests that build_remote_path handles root directory correctly.
# Calls onedrive_download.build_remote_path.
def test_build_remote_path_root() -> None:
    item = {
        'name': 'RootFile.xlsx',
        'parentReference': {'path': '/drive/root:'}
    }
    result = onedrive_download.build_remote_path(item)
    assert result.endswith('RootFile.xlsx')


# Tests that needs_download detects missing manifest entry.
# Calls onedrive_download.needs_download.
def test_needs_download_missing_entry() -> None:
    assert onedrive_download.needs_download(None, 'etag123', '2026-02-16T00:00:00Z', 1000) is True


# Tests that needs_download detects eTag changes.
# Calls onedrive_download.needs_download.
def test_needs_download_etag_changed() -> None:
    existing = {
        'etag': 'old_etag',
        'last_modified': '2026-02-16T00:00:00Z',
        'size': 1000,
    }
    result = onedrive_download.needs_download(
        existing, 'new_etag', '2026-02-16T00:00:00Z', 1000
    )
    assert result is True


# Tests that needs_download detects size changes when eTag unavailable.
# Calls onedrive_download.needs_download.
def test_needs_download_size_changed_no_etag() -> None:
    existing = {
        'etag': '',
        'last_modified': '2026-02-16T00:00:00Z',
        'size': 1000,
    }
    result = onedrive_download.needs_download(
        existing, '', '2026-02-16T00:00:00Z', 2000
    )
    assert result is True


# Tests that needs_download returns False when nothing changed.
# Calls onedrive_download.needs_download.
def test_needs_download_unchanged() -> None:
    existing = {
        'etag': 'etag123',
        'last_modified': '2026-02-16T00:00:00Z',
        'size': 1000,
    }
    result = onedrive_download.needs_download(
        existing, 'etag123', '2026-02-16T00:00:00Z', 1000
    )
    assert result is False


# Tests that resolve_folder_list_url builds correct URL for root.
# Calls onedrive_download.resolve_folder_list_url.
def test_resolve_folder_list_url_root(tmp_path: Path) -> None:
    config = onedrive_download.SyncConfig(
        client_id='test',
        tenant='consumers',
        remote_folder='/',
        download_dir=tmp_path,
        manifest_path=tmp_path / 'manifest.xlsx',
        token_cache_path=tmp_path / 'token.bin',
        flatten=True,
        recursive=True,
        dry_run=False,
        interactive=False,
    )
    result = onedrive_download.resolve_folder_list_url(config)
    assert 'me/drive/root/children' in result


# Tests that resolve_folder_list_url builds correct URL for subfolder.
# Calls onedrive_download.resolve_folder_list_url.
def test_resolve_folder_list_url_subfolder(tmp_path: Path) -> None:
    config = onedrive_download.SyncConfig(
        client_id='test',
        tenant='consumers',
        remote_folder='/MyFolder/SubFolder',
        download_dir=tmp_path,
        manifest_path=tmp_path / 'manifest.xlsx',
        token_cache_path=tmp_path / 'token.bin',
        flatten=True,
        recursive=True,
        dry_run=False,
        interactive=False,
    )
    result = onedrive_download.resolve_folder_list_url(config)
    assert 'MyFolder/SubFolder' in result
    assert 'root:/' in result


class _DummyResponse:
    def __init__(self, chunks: list[bytes] | None = None, fail_on_iter: bool = False) -> None:
        self._chunks = chunks or []
        self._fail_on_iter = fail_on_iter

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        if self._fail_on_iter:
            raise requests.exceptions.ConnectionError("simulated stream failure")
        for chunk in self._chunks:
            yield chunk


class _DummySession:
    def __init__(self, response: _DummyResponse) -> None:
        self.response = response
        self.called = False

    def get(self, url: str, stream: bool, timeout: int):
        self.called = True
        return self.response


# Tests that download_file uses provided session and computes SHA256 hash.
# Calls onedrive_download.download_file.
def test_download_file_uses_session(tmp_path: Path) -> None:
    local_path = tmp_path / "downloaded.csv"
    chunks = [b"abc", b"123"]
    session = _DummySession(_DummyResponse(chunks=chunks))

    sha = onedrive_download.download_file("https://example.com/file", local_path, session=session)

    assert session.called is True
    assert local_path.read_bytes() == b"".join(chunks)
    assert sha == hashlib.sha256(b"".join(chunks)).hexdigest()


# Tests that download_file cleans up partial file and raises RuntimeError on stream failure.
# Calls onedrive_download.download_file.
def test_download_file_cleans_temp_on_failure(tmp_path: Path) -> None:
    local_path = tmp_path / "broken.xlsx"
    session = _DummySession(_DummyResponse(fail_on_iter=True))

    with pytest.raises(RuntimeError, match="Failed to download file"):
        onedrive_download.download_file("https://example.com/file", local_path, session=session)

    assert not local_path.exists()
    assert not local_path.with_suffix(local_path.suffix + ".part").exists()
