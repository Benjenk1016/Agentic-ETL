# backend/src/google_to_onedrive_sync.py
"""
Runs the Google Sheets -> OneDrive export/sync.

This module is designed to be executed as:
  python -m backend.src.google_to_onedrive_sync
so the FastAPI run_module() helper can call it.
"""

from __future__ import annotations

import sys

from backend.src.GoogleOnedrive import run_download_process


def main() -> int:
  success, message = run_download_process()
  print(message)
  return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
