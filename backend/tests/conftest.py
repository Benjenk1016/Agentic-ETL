import sys
import shutil
from pathlib import Path
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session", autouse=True)
def cleanup_prompt_artifacts_after_pytest():
    """Remove response artifacts leaked to repo data/responses during this pytest session."""
    prompts_dir = PROJECT_ROOT / "data" / "responses"
    archive_dir = prompts_dir / "archive"
    patterns = ("llm_*.json", "prepared_*.json")

    before = set()
    for folder in (prompts_dir, archive_dir):
        if not folder.exists():
            continue
        for pattern in patterns:
            before.update(path.resolve() for path in folder.glob(pattern))

    yield

    for folder in (prompts_dir, archive_dir):
        if not folder.exists():
            continue
        for pattern in patterns:
            for path in folder.glob(pattern):
                resolved = path.resolve()
                if resolved in before:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


@pytest.fixture(autouse=True)
def isolate_prompt_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Route response artifacts to test-local temp dirs and clean them after each test."""
    prompts_dir = tmp_path / "responses"
    archive_dir = prompts_dir / "archive"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    def _test_prompt_dirs() -> tuple[Path, Path]:
        prompts_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)
        return prompts_dir, archive_dir

    import backend.api.llm_prompt as llm_prompt
    import backend.api.app as api_app

    monkeypatch.setattr(llm_prompt, "ensure_prompt_directories", _test_prompt_dirs)
    monkeypatch.setattr(api_app, "ensure_prompt_directories", _test_prompt_dirs)

    try:
        yield
    finally:
        shutil.rmtree(prompts_dir, ignore_errors=True)