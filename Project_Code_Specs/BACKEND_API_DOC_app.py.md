# Backend API & Function Documentation (`app.py`)

This document provides a reference for the backend endpoints and functions defined in `app.py` and related backend modules, including:

- Endpoint usage status (used by frontend, indirect/internal, unused/legacy)
- Relationship to frontend (API usage)
- Functional breakdown of backend logic

---

# API Endpoints

## Health & Diagnostics

- **`GET /health`**
  - Used by: Frontend health check
  - Status: Used
  - Purpose: Service status check

- **`GET /llm/startup-diagnostics`**
  - Used by: Frontend diagnostics
  - Status: Used
  - Purpose: LLM warmup status

---

## File Processing & Change Detection

- **`POST /run/file-processing`**
  - Used by: Frontend (file scan/prepare)
  - Status: Used
  - Purpose: Scans input files, detects changes, updates manifest

---

## Column Extraction

- **`POST /run/extract-columns`**
  - Used by: Frontend (column extraction)
  - Status: Used
  - Purpose: Extracts column headers and metadata from Excel file

---

## Analysis (LLM)

- **`POST /responses/pending`**
  - Used by: Frontend (pending prompt records)
  - Status: Used
  - Purpose: Lists pending LLM prompt records

---

## Response Processing

- **`POST /responses/archive`**
  - Used by: Frontend (archive prompt)
  - Status: Used

- **`POST /responses/auto-handle/preview`**
  - Used by: Frontend (preview auto-handle)
  - Status: Used

- **`POST /responses/auto-handle/execute`**
  - Used by: Frontend (apply auto-handle)
  - Status: Used

---

## File Sync & Integrations

- **`POST /run/file-sync`**
  - Used by: Frontend (full sync)
  - Status: Used

- **`POST /run/onedrive_download`**
  - Used by: Frontend (OneDrive sync)
  - Status: Used

- **`POST /run/onedrive_auth/start`**
  - Used by: Frontend (OneDrive auth start)
  - Status: Used

- **`POST /run/onedrive_auth/complete`**
  - Used by: Frontend (OneDrive auth complete)
  - Status: Used

---

## Cleanup

- **`POST /run/cleanup`**
  - Used by: Frontend (reset/cleanup)
  - Status: Used

---


## Legacy/Commented Out Endpoints

- **`POST /run/validate`**
  - Status: Commented out (unused/deprecated)

- **`POST /run/transform`**
  - Status: Commented out (unused/deprecated)

---

# Function Breakdown

Functions are grouped by responsibility within `app.py` and related backend modules.

---

## 1. File Processing & Change Detection

- `process_all_input_files_and_update_manifest`
  - Scans input directory, detects changed files, processes them, updates manifest

- `clear_directory`
  - Deletes all files in a directory

---

## 2. Column Extraction

- `extract_columns` (from column_extract.py)
  - Extracts column headers and metadata from Excel files

- `extract_data_rows` (from column_extract.py)
  - Extracts data rows for config sync

---

## 3. Analysis & LLM Integration

- `query_llm`, `query_llm_stream`, `warmup_llm` (from llm_prompt.py)
  - Handles LLM prompt construction, streaming, and warmup

- `ensure_prompt_directories`, `archive_prompt_record`, `save_prepared_prompt_record`, `save_combined_response_record` (from llm_prompt.py)
  - Manages prompt/response record storage

---

## 4. Response Processing

- `_run_prompt_auto_handle`, `_run_ai_response_append`, `_extract_record_append_context`
  - Handles LLM prompt records and applies mappings

---

## 5. File Sync & Integrations

- `run_module`
  - Runs a Python module as a subprocess for modular backend tasks

- `load_token_cache`, `save_token_cache` (from onedrive_download.py)
  - Handles OneDrive token cache

---

## 6. Utility & Config

- Directory/path utilities: `default_input_dir`, `default_output_dir`, `default_hash_dir`, `default_state_dir`, `default_archive_dir` (from etl_utils.py)
  - Used throughout backend for consistent paths

---


# Unused / Legacy Functions

The following are not used by the frontend and have been commented out:

- `/run/validate` (commented out)
- `/run/transform` (commented out)

---

# Indirect / Internal Functions

These are used internally or by other endpoints:

- `_resolve_prompt_record_path`, `_parse_onedrive_sync_counts`, `_parse_google_sync_counts`, `_parse_processing_counts`
- Many utility functions in backend/src are used only by backend logic

---

# Summary

The backend in `app.py` is centered around:

- File processing and change detection
- Column extraction
- LLM analysis and prompt/response management
- Response processing and config updates
- File synchronization (OneDrive, Google Drive)
- Cleanup/reset

All active endpoints are used by the frontend except for legacy endpoints, which are safe to remove. Most backend/src modules are used by the API or each other. No significant unused code detected.

---

## 5. File Processing & Cleanup

- `run_cleanup`
  - Clears generated files and state

- `run_validate`
  - Deprecated validation endpoint

- `run_transform`
  - Deprecated transform endpoint

---

## 6. File Sync

- `run_file_sync`
  - Executes full sync pipeline

- `run_google_to_onedrive`
  - Alias for sync process

- `run_onedrive_download`
  - Internal sync step

---

## 7. Config Management

- `upload_config`
  - Handles config file upload

---


# Unused / Legacy Functions

The following are not used by the frontend and have been commented out:

- `extract_columns_file`
- `legacy_append_mappings`
- `run_validate`
- `run_transform`

---

# Indirect / Internal Functions

These are used internally or by other endpoints:

- `extract_columns`
- `run_append_mappings`
- `run_onedrive_download`
- `auto_handle_preview`

---

# Summary

The backend in `app.py` is centered around:

- File upload and change detection
- AI analysis execution
- Response processing and config updates
- File synchronization

Most active functionality is concentrated in:

- `/upload_and_process`
- `/check_file_changes`
- `/analyze-excel_files-from-input`
- `/responses/*`
- `/run/file-sync`

Unused and legacy endpoints are minimal but present and can be reviewed for removal.