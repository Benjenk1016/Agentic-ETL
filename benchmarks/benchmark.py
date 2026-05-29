#!/usr/bin/env python3
"""
Model Benchmarking Script for Ollama.

Automatically discovers and benchmarks all installed Ollama models using a
single ETL prompt case. Measures RAM usage, inference time, and response
quality for one standardized mapping scenario.

Usage:
    python benchmark.py
"""
import os
import json
import fnmatch
import re
import socket
import sys
import time
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

import pandas as pd

# ============================================================================
# CONFIGURATION
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = DATA_DIR / "config"
INPUT_DIR_CANDIDATES = [DATA_DIR / "input"]
ANALYZE_MAX_WORDS = int(os.getenv("ANALYZE_MAX_WORDS", "1150"))


# Explicit benchmark source targeting requested ETL test files.
CONFIG_FILE_PATTERN = "ETL Configs Example*"
INPUT_FILE_PATTERN = "ETL Inputs*"

# Allow importing backend utilities when script is run as python benchmarks/benchmark.py
sys.path.insert(0, str(PROJECT_ROOT))

from backend.src.column_extract import extract_columns
from backend.api.analysis_utils import compare_payload_columns, format_payload_for_llm

# Ollama connection settings
OLLAMA_URL = "http://localhost:11434"

# Timeout thresholds based on model size (increased for harder prompt tiers)
TIMEOUT_SMALL_MODEL_MB = 2000  # Models < 2GB
TIMEOUT_MEDIUM_MODEL_MB = 5000  # Models 2-5GB
TIMEOUT_SMALL_SEC = 120
TIMEOUT_MEDIUM_SEC = 240
TIMEOUT_LARGE_SEC = 600

# Timeout multiplier for the single benchmark prompt
PROMPT_TIMEOUT_MULTIPLIER = {
    "single": 1.0,
}

# Timeout handling tuning
PROMPT_TIMEOUT_BACKOFF_MULTIPLIER = 1.4  # Retry timeout expansion after first timeout
PROMPT_TIMEOUT_MAX_RETRIES = 1
PROMPT_CHAR_TIMEOUT_DIVISOR = 250  # Add seconds based on prompt size
RESPONSE_VALIDATION_MAX_RETRIES = 1

# Warm-up settings to avoid counting model load/compile time as prompt timeout
MODEL_WARMUP_TIMEOUT_SEC = 180
MODEL_WARMUP_PROMPT = "Reply with exactly: OK"

# Report generation settings
MAX_CASE_ERRORS_DISPLAY = 3  # Show first N errors, summarize rest

# Cooldown between model tests to let system stabilize
MODEL_COOLDOWN_SEC = 5

# Models to skip by default for now (large or unstable for local benchmarking)
SKIP_MODEL_PATTERNS = [
    "tinyllama:1.1b", "phi:2.7b", "mistral:7b", "phi3:3.8b", "llama2:7b",
]

# Token estimation (rough approximation: 1 token ≈ 4 characters)
CHARS_PER_TOKEN = 4

# Number of input/config pairs used per benchmark run
PROMPT_CASE_LIMIT = 1

# Payload sources for benchmark cases. JSON files must match extract_columns output shape.
SUPPORTED_PAYLOAD_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".json", ".csv"}

MODEL_SEED = 42
MODEL_TEMPERATURE = 0.0

# ============================================================================
# DATA CLASSES
# ============================================================================

class BenchmarkResult:
    """
    Stores metrics for a single model benchmark run.
    """
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.file_size_mb = 0.0
        self.inference_time_sec = 0.0
        self.avg_prompt_time_sec = 0.0
        self.tokens_per_sec = 0.0
        self.response = ""
        self.error: Optional[str] = None
        self.prompt_runs: list[dict] = []


# ============================================================================
# TEST DATA LOADING
# ============================================================================

# Detects input directory (supports both 'input' and 'inputs' naming)
def detect_input_cases_dir() -> Optional[Path]:
    """
    Find test case directory, checking common naming variations.
    Returns None if no valid directory found.
    """
    for candidate in INPUT_DIR_CANDIDATES:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def list_payload_files(directory: Path) -> list[Path]:
    """Return sorted payload source files from a directory."""
    if not directory.exists() or not directory.is_dir():
        return []
    return [
        file_path
        for file_path in sorted(directory.iterdir())
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_PAYLOAD_EXTENSIONS
    ]


def extract_excel_payload(excel_path: Path) -> dict[str, Any]:
    """Load extract_columns payload from an Excel file as dict."""
    payload_raw = extract_columns(str(excel_path))
    return json.loads(payload_raw)


def load_payload(payload_path: Path) -> dict[str, Any]:
    """Load extract_columns-style payload from Excel or JSON file."""
    suffix = payload_path.suffix.lower()

    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return extract_excel_payload(payload_path)

    if suffix == ".json":
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(
                f"JSON payload must be an object/dict (file: {payload_path.name})."
            )
        return payload

    if suffix == ".csv":
        df = pd.read_csv(payload_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp_file:
            temp_excel_path = Path(temp_file.name)
        try:
            df.to_excel(temp_excel_path, index=False)
            return extract_excel_payload(temp_excel_path)
        finally:
            if temp_excel_path.exists():
                temp_excel_path.unlink()

    raise ValueError(
        f"Unsupported payload file type for benchmark: {payload_path.suffix}"
    )


def summarize_comparison(comparison: dict[str, Any]) -> str:
    """Build a compact comparison summary for prompt context."""
    shared = comparison.get("shared_columns", [])
    missing = comparison.get("missing_in_input", [])
    extra = comparison.get("extra_in_input", [])
    overlap_ratio = comparison.get("overlap_ratio_vs_config", 0)

    return (
        "AUTOMATED DETECTION STARTER:\n"
        f"- overlap ratio vs config: {overlap_ratio}\n"
        f"- shared columns ({len(shared)}): {shared[:20]}\n"
        f"- missing in input ({len(missing)}): {missing[:20]}\n"
        f"- extra in input ({len(extra)}): {extra[:20]}"
    )


def extract_sheet_names_from_payload(payload: dict[str, Any]) -> list[str]:
    """Extract sheet names from extract_columns payload keys."""
    sheet_names: list[str] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        if "(" not in key or not key.endswith(")"):
            continue
        sheet_names.append(key.split("(", 1)[1].rstrip(")").strip())
    return sheet_names


def normalize_token(value: str) -> str:
    """Normalize text for robust comparisons across spacing/case variants."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def parse_output_blocks(response_text: str) -> list[dict[str, str]]:
    """Parse OUTPUT blocks from model response into key/value dictionaries."""
    marker_pattern = re.compile(
        r"^\s*OUTPUT\s*:\s*suggested CONFIG row appendment\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    matches = list(marker_pattern.finditer(response_text or ""))
    if not matches:
        return []

    blocks: list[dict[str, str]] = []
    for idx, match in enumerate(matches):
        block_start = match.end()
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(response_text)
        block_text = response_text[block_start:block_end]
        block_fields: dict[str, str] = {}

        for raw_line in block_text.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            block_fields[key.strip().lower()] = value.strip()

        blocks.append(block_fields)

    return blocks


def validate_mapping_response(
    response_text: str,
    expected_input_sheets: list[str],
    expected_source_wb_keyword: str,
) -> tuple[bool, str]:
    """Validate model response shape and sheet/workbook coverage constraints."""
    blocks = parse_output_blocks(response_text)
    if not blocks:
        return False, "No OUTPUT blocks found"

    expected_count = len(expected_input_sheets)
    if len(blocks) != expected_count:
        return (
            False,
            f"Expected exactly {expected_count} OUTPUT blocks but found {len(blocks)}",
        )

    expected_sheet_norms = {normalize_token(name) for name in expected_input_sheets}
    observed_sheet_norms: list[str] = []
    observed_workbook_norms: list[str] = []

    for block in blocks:
        source_ws = block.get("source_ws", "")
        source_wb_keyword = block.get("source_wb_keyword", "")
        if not source_ws:
            return False, "At least one OUTPUT block is missing source_ws"
        if not source_wb_keyword:
            return False, "At least one OUTPUT block is missing source_wb_keyword"
        observed_sheet_norms.append(normalize_token(source_ws))
        observed_workbook_norms.append(normalize_token(source_wb_keyword))

    if len(observed_sheet_norms) != len(set(observed_sheet_norms)):
        return False, "source_ws contains duplicates; expected one mapping per sheet"

    observed_sheet_set = set(observed_sheet_norms)
    missing = sorted(expected_sheet_norms - observed_sheet_set)
    extras = sorted(observed_sheet_set - expected_sheet_norms)
    if missing or extras:
        return (
            False,
            f"source_ws mismatch (missing={missing if missing else '[]'}, extra={extras if extras else '[]'})",
        )

    expected_wb_norm = normalize_token(expected_source_wb_keyword)
    mismatched_workbooks = [wb for wb in observed_workbook_norms if wb != expected_wb_norm]
    if mismatched_workbooks:
        return (
            False,
            "source_wb_keyword must match the current input workbook for every OUTPUT block",
        )

    return True, "OK"


def build_analysis_prompt(
    config_file: Path,
    input_file: Path,
    config_payload: dict[str, Any],
    input_payload: dict[str, Any],
) -> str:
    """Build benchmark prompt aligned with the analyze endpoint prompt style."""
    comparison = compare_payload_columns(input_payload, config_payload)
    input_payload_str = format_payload_for_llm(input_payload, input_file.name, "INPUT")
    config_payload_str = format_payload_for_llm(config_payload, config_file.name, "CONFIG")
    comparison_summary = summarize_comparison(comparison)
    input_sheet_names = extract_sheet_names_from_payload(input_payload)
    input_sheet_names_line = ", ".join(input_sheet_names) if input_sheet_names else "(none)"
    expected_source_wb_keyword = input_file.stem

    return f"""You are an ETL mapping expert. Map INPUT sheets to CONFIG format. Produce one OUTPUT block per INPUT sheet — no extra text.

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


def build_difficulty_prompts() -> list[dict]:
    """Build benchmark prompt cases for all matching ETL input test files."""
    config_files = [
        file_path
        for file_path in list_payload_files(CONFIG_DIR)
        if fnmatch.fnmatch(file_path.name, CONFIG_FILE_PATTERN)
    ]
    if not config_files:
        raise RuntimeError(
            "No matching config payload source found. "
            f"Expected file pattern '{CONFIG_FILE_PATTERN}' in {CONFIG_DIR}."
        )

    input_cases_dir = detect_input_cases_dir()
    if not input_cases_dir:
        raise RuntimeError("Input directory not found. Expected data/input or data/inputs.")

    input_files = [
        file_path
        for file_path in list_payload_files(input_cases_dir)
        if fnmatch.fnmatch(file_path.name, INPUT_FILE_PATTERN)
    ]
    if not input_files:
        raise RuntimeError(
            "No matching input payload source found. "
            f"Expected file pattern '{INPUT_FILE_PATTERN}' in {input_cases_dir}."
        )

    config_file = config_files[0]
    config_payload = load_payload(config_file)

    prompt_suite = []
    for input_file in input_files:
        input_payload = load_payload(input_file)
        expected_input_sheets = extract_sheet_names_from_payload(input_payload)
        prompt_suite.append(
            {
                "level": "single",
                "label": f"Single ({input_file.name} vs {config_file.name})",
                "prompt": build_analysis_prompt(
                    config_file=config_file,
                    input_file=input_file,
                    config_payload=config_payload,
                    input_payload=input_payload,
                ),
                "input_file": input_file.name,
                "config_file": config_file.name,
                "expected_input_sheets": expected_input_sheets,
                "expected_source_wb_keyword": input_file.stem,
            }
        )

    return prompt_suite

def check_ollama_running():
    """Verify Ollama is running locally."""
    import requests
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if response.status_code == 200:
            print(f"✅ Ollama is running at {OLLAMA_URL}")
            return True
    except Exception:
        pass
    print("❌ Ollama is not running!")
    print(f"Start Ollama on your machine and ensure it's accessible at {OLLAMA_URL}")
    print("Visit: https://ollama.ai/download to download Ollama")
    exit(1)

def list_models():
    """Get list of installed models from Ollama."""
    import requests
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        if response.status_code == 200:
            data = response.json()
            models = [model["name"] for model in data.get("models", [])]
            return models
    except Exception as e:
        print(f"Warning: Could not fetch model list: {e}")
    return []


def filter_models(installed_models: list[str]) -> tuple[list[str], list[str]]:
    """
    Filter installed models using built-in skip patterns.
    Returns (selected_models, skipped_models).
    """
    selected_models = []
    skipped_models = []

    for model in installed_models:
        if any(fnmatch.fnmatch(model, pattern) for pattern in SKIP_MODEL_PATTERNS):
            skipped_models.append(model)
            continue
        selected_models.append(model)

    return selected_models, skipped_models

def get_model_size(model_name):
    """Get the model file size in MB."""
    import requests
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        if response.status_code == 200:
            data = response.json()
            for model in data.get("models", []):
                if model["name"] == model_name:
                    # size is in bytes
                    size_mb = model.get("size", 0) / (1024 * 1024)
                    return size_mb
    except Exception as e:
        print(f"Warning: Could not fetch model size: {e}")
    return 0

def unload_model(model_name):
    """Explicitly unload a model from Ollama to free memory."""
    import requests
    try:
        payload = {
            "model": model_name,
            "keep_alive": 0  # Immediately unload the model
        }
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=10
        )
        if response.status_code == 200:
            print(f"   🧹 Unloaded {model_name} from memory")
            return True
    except Exception as e:
        print(f"   ⚠️  Could not unload model: {e}")
    return False


def warmup_model(model_name: str):
    """Warm up model once so first benchmark prompt is less likely to timeout."""
    import requests

    payload = {
        "model": model_name,
        "prompt": MODEL_WARMUP_PROMPT,
        "stream": False,
        "options": {
            "num_predict": 8,
            "temperature": 0,
        },
    }

    try:
        requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=(10, MODEL_WARMUP_TIMEOUT_SEC),
        )
        print("🧪 Warm-up: completed")
    except Exception as e:
        print(f"⚠️  Warm-up skipped due to error: {str(e)[:120]}")

def benchmark_model(model_name, prompt_suite):
    """Benchmark a single model."""
    result = BenchmarkResult(model_name)
    
    print(f"\n{'='*60}")
    print(f"Benchmarking: {model_name}")
    print(f"{'='*60}")
    
    # Get model size
    result.file_size_mb = get_model_size(model_name)
    print(f"📊 Model size: {result.file_size_mb:.1f} MB")

    # Warm up model first to avoid first-prompt timeout caused by load/compile latency
    warmup_model(model_name)
    
    # Calculate adaptive timeout based on model size
    # Small models (<2GB): 60s, Medium (2-5GB): 120s, Large (5GB+): 300s
    if result.file_size_mb < TIMEOUT_SMALL_MODEL_MB:
        inference_timeout = TIMEOUT_SMALL_SEC
    elif result.file_size_mb < TIMEOUT_MEDIUM_MODEL_MB:
        inference_timeout = TIMEOUT_MEDIUM_SEC
    else:
        inference_timeout = TIMEOUT_LARGE_SEC
    
    print(f"⏱️  Base timeout: {inference_timeout}s (adaptive based on model size)")
    print(f"🧪 Prompt cases: {len(prompt_suite)} (single)")
    
    total_time = 0.0
    total_tokens = 0.0
    successful_cases = 0
    case_errors = []
    response_snippets = []

    for prompt_case in prompt_suite:
        case_name = prompt_case["label"]
        prompt_level = prompt_case["level"]
        prompt = prompt_case["prompt"]
        expected_input_sheets = prompt_case.get("expected_input_sheets", [])
        expected_source_wb_keyword = prompt_case.get("expected_source_wb_keyword", "")
        prompt_size_boost = len(prompt) // PROMPT_CHAR_TIMEOUT_DIVISOR
        case_timeout = int(
            inference_timeout * PROMPT_TIMEOUT_MULTIPLIER[prompt_level]
            + prompt_size_boost
        )

        print(f"🔄 Running inference for prompt: {case_name} ({prompt_level})")
        print(f"   ⏱️  Prompt timeout: {case_timeout}s")
        case_elapsed_total = 0.0
        response_attempt = 0
        final_case_response = ""
        final_effective_timeout = case_timeout
        case_failed = False

        try:
            import requests

            while response_attempt <= RESPONSE_VALIDATION_MAX_RETRIES:
                current_prompt = prompt
                if response_attempt > 0:
                    expected_sheets_text = ", ".join(expected_input_sheets) if expected_input_sheets else "(none)"
                    current_prompt = (
                        f"{prompt}\n\n"
                        "CRITICAL RETRY INSTRUCTIONS:\n"
                        "- Regenerate from scratch; ignore previous attempts.\n"
                        f"- Return exactly {len(expected_input_sheets)} OUTPUT blocks.\n"
                        f"- source_wb_keyword must be exactly: {expected_source_wb_keyword}.\n"
                        f"- Use each source_ws exactly once from: {expected_sheets_text}.\n"
                        "- Do not include any workbook or sheet names not listed above.\n"
                    )

                payload = {
                    "model": model_name,
                    "prompt": current_prompt,
                    "stream": False,
                    "options": {
                        "num_predict": 512,
                        "temperature": MODEL_TEMPERATURE,
                        "seed": 42,
                        "top_p": 1.0,
                        "top_k": 1,
                    },
                }
                rc = 1
                stdout = ""
                stderr = ""
                effective_timeout = case_timeout
                attempt_start_time = time.time()

                for attempt in range(PROMPT_TIMEOUT_MAX_RETRIES + 1):
                    try:
                        response = requests.post(
                            f"{OLLAMA_URL}/api/generate",
                            json=payload,
                            timeout=(10, effective_timeout),
                        )
                        stdout = response.text
                        stderr = ""
                        rc = 0 if response.status_code == 200 else 1
                        break
                    except (requests.exceptions.Timeout, TimeoutError, socket.timeout):
                        actual_time = time.time() - attempt_start_time
                        stdout = ""
                        stderr = f"REQUEST_TIMEOUT after {actual_time:.1f}s"
                        rc = 1
                        if attempt < PROMPT_TIMEOUT_MAX_RETRIES:
                            effective_timeout = int(
                                effective_timeout * PROMPT_TIMEOUT_BACKOFF_MULTIPLIER
                            )
                            print(
                                f"   🔁 Retrying after timeout with higher limit: {effective_timeout}s"
                            )
                            continue
                        break
                    except requests.exceptions.RequestException as e:
                        stdout = ""
                        stderr = f"REQUEST_ERROR: {str(e)}"
                        rc = 1
                        break

                attempt_elapsed = time.time() - attempt_start_time
                case_elapsed_total += attempt_elapsed
                final_effective_timeout = effective_timeout

                if rc != 0:
                    if "REQUEST_TIMEOUT" in stderr:
                        print(f"   ⏱️  TIMEOUT: {stderr} (limit: {effective_timeout}s)")
                        case_errors.append(f"{case_name}: timed out")
                        result.prompt_runs.append({
                            "level": prompt_level,
                            "label": case_name,
                            "timeout_sec": effective_timeout,
                            "elapsed_sec": case_elapsed_total,
                            "tokens": 0.0,
                            "tokens_per_sec": 0.0,
                            "response": "",
                            "error": "timed out",
                        })
                    elif "REQUEST_ERROR" in stderr:
                        print(f"   ❌ REQUEST ERROR: {stderr}")
                        case_errors.append(f"{case_name}: request error")
                        result.prompt_runs.append({
                            "level": prompt_level,
                            "label": case_name,
                            "timeout_sec": effective_timeout,
                            "elapsed_sec": case_elapsed_total,
                            "tokens": 0.0,
                            "tokens_per_sec": 0.0,
                            "response": "",
                            "error": stderr,
                        })
                    else:
                        print("   ❌ API ERROR")
                        case_errors.append(f"{case_name}: API error")
                        result.prompt_runs.append({
                            "level": prompt_level,
                            "label": case_name,
                            "timeout_sec": effective_timeout,
                            "elapsed_sec": case_elapsed_total,
                            "tokens": 0.0,
                            "tokens_per_sec": 0.0,
                            "response": "",
                            "error": "API error",
                        })
                    case_failed = True
                    break

                try:
                    data = json.loads(stdout)
                    case_response = data.get("response", "")
                except json.JSONDecodeError:
                    print("   ❌ Invalid JSON response from model")
                    case_errors.append(f"{case_name}: invalid JSON response")
                    result.prompt_runs.append({
                        "level": prompt_level,
                        "label": case_name,
                        "timeout_sec": effective_timeout,
                        "elapsed_sec": case_elapsed_total,
                        "tokens": 0.0,
                        "tokens_per_sec": 0.0,
                        "response": stdout,
                        "error": "invalid JSON response",
                    })
                    case_failed = True
                    break

                is_valid, validation_message = validate_mapping_response(
                    case_response,
                    expected_input_sheets,
                    expected_source_wb_keyword,
                )
                if is_valid:
                    final_case_response = case_response
                    break

                print(
                    "   ⚠️  Output validation failed: "
                    f"{validation_message}"
                )
                if response_attempt < RESPONSE_VALIDATION_MAX_RETRIES:
                    response_attempt += 1
                    print(
                        "   🔁 Retrying prompt with strict correction "
                        f"({response_attempt}/{RESPONSE_VALIDATION_MAX_RETRIES})"
                    )
                    continue

                case_errors.append(f"{case_name}: output validation failed")
                result.prompt_runs.append({
                    "level": prompt_level,
                    "label": case_name,
                    "timeout_sec": effective_timeout,
                    "elapsed_sec": case_elapsed_total,
                    "tokens": 0.0,
                    "tokens_per_sec": 0.0,
                    "response": case_response,
                    "error": f"output validation failed ({validation_message})",
                })
                case_failed = True
                break

            if case_failed:
                continue

            if not final_case_response:
                case_errors.append(f"{case_name}: empty validated response")
                result.prompt_runs.append({
                    "level": prompt_level,
                    "label": case_name,
                    "timeout_sec": final_effective_timeout,
                    "elapsed_sec": case_elapsed_total,
                    "tokens": 0.0,
                    "tokens_per_sec": 0.0,
                    "response": "",
                    "error": "empty validated response",
                })
                continue

            case_response = final_case_response
            inference_time = case_elapsed_total

            # Extract first 2 sentences for console output
            sentences = case_response.replace('\n', ' ').split('. ')
            preview = '. '.join(sentences[:2])
            if len(sentences) > 2:
                preview += '...'
            print(f"   ✅ Completed in {inference_time:.2f}s")
            print(f"   💬 Response: {preview[:200]}")

            response_length = len(case_response)
            estimated_tokens = response_length / CHARS_PER_TOKEN
            prompt_tokens_per_sec = estimated_tokens / max(inference_time, 0.001)
            total_tokens += estimated_tokens
            total_time += inference_time
            successful_cases += 1
            response_snippets.append(f"[{case_name}] {case_response[:120]}")
            result.prompt_runs.append({
                "level": prompt_level,
                "label": case_name,
                "timeout_sec": effective_timeout,
                "elapsed_sec": inference_time,
                "tokens": estimated_tokens,
                "tokens_per_sec": prompt_tokens_per_sec,
                "response": case_response,
                "error": None,
            })
        except Exception as e:
            case_errors.append(f"{case_name}: unexpected error ({str(e)[:60]})")
            result.prompt_runs.append({
                "level": prompt_level,
                "label": case_name,
                "timeout_sec": final_effective_timeout,
                "elapsed_sec": case_elapsed_total,
                "tokens": 0.0,
                "tokens_per_sec": 0.0,
                "response": "",
                "error": f"unexpected error ({str(e)[:60]})",
            })

    if successful_cases == 0:
        result.error = "; ".join(case_errors[:3])
        if len(case_errors) > 3:
            result.error += f"; +{len(case_errors) - 3} more"
        return result

    result.avg_prompt_time_sec = total_time / successful_cases
    result.inference_time_sec = total_time
    result.tokens_per_sec = total_tokens / total_time if total_time > 0 else 0
    result.response = "\n".join(response_snippets[:2])
    if case_errors:
        result.response += f"\n(Partial failures: {len(case_errors)}/{len(prompt_suite)} prompts)"
    
    # Print results
    print(f"✅ Total inference time: {result.inference_time_sec:.2f}s")
    print(f"✅ Average prompt time: {result.avg_prompt_time_sec:.2f}s")
    print(f"📈 Tokens/sec: {result.tokens_per_sec:.2f}")
    print(f"📝 Response preview: {result.response[:100]}...")
    
    return result

def generate_report(results, prompt_suite):
    """Generate a markdown report of benchmark results."""
    report = f"""# Model Benchmark Report
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Summary
This benchmark tested {len(results)} models on the same hardware and a fixed
set of prompt cases, run sequentially.
"""

    report += "\n## Prompts Used (Exact)\n\n"
    report += "The following are the exact prompts used for this benchmark run.\n\n"

    for prompt_case in prompt_suite:
        report += f"### {prompt_case['label']} ({prompt_case['level']})\n\n"
        report += "```text\n"
        report += f"{prompt_case['prompt']}\n"
        report += "```\n\n"
    
    report += "\n## Detailed Results\n\n"
    
    for r in results:
        report += f"### {r.model_name}\n"
        if r.error:
            report += f"**Error**: {r.error}\n\n"
        else:
            report += f"- File size: {r.file_size_mb:.0f} MB\n"
            report += f"- Total inference time (all prompts): {r.inference_time_sec:.2f}s\n"
            report += f"- Average prompt time: {r.avg_prompt_time_sec:.2f}s\n"
            report += f"- Tokens/sec: {r.tokens_per_sec:.2f}\n"
            report += "\n"

            for run in r.prompt_runs:
                report += f"#### Prompt: {run['label']} ({run['level']})\n"
                report += f"- Timeout: {run['timeout_sec']}s\n"
                report += f"- Elapsed: {run['elapsed_sec']:.2f}s\n"
                report += f"- Tokens/sec (estimated): {run['tokens_per_sec']:.2f}\n"
                if run["error"]:
                    report += f"- Status: ERROR ({run['error']})\n\n"
                else:
                    report += "- Status: OK\n"
                    report += "- Full response:\n\n"
                    report += "```text\n"
                    report += f"{run['response']}\n"
                    report += "```\n\n"

    report += "## Per-Prompt Timing Matrix\n\n"
    report += "| Model | Single Prompt (s) |\n"
    report += "|-------|--------------------|\n"

    for r in results:
        single_time = "-"
        for run in r.prompt_runs:
            if run["level"] == "single":
                single_time = f"{run['elapsed_sec']:.2f}"
        report += f"| {r.model_name} | {single_time} |\n"
    
    return report

def main():
    print("🚀 Starting Model Benchmark Suite")
    print("=" * 60)
    
    # Check prerequisites
    check_ollama_running()
    
    # Build prompt suite from real config/input Excel files
    try:
        prompt_suite = build_difficulty_prompts()
    except Exception as e:
        print(f"\n❌ Could not build benchmark prompts: {e}")
        print("   Ensure data/config has at least one Excel file and data/input has Excel cases.")
        exit(1)

    print("🧪 Loaded app-aligned benchmark prompt cases")
    for prompt_case in prompt_suite:
        print(
            "   - "
            f"{prompt_case['label']}"
        )

    # Get all installed models dynamically
    installed_models = list_models()
    
    if not installed_models:
        print("\n❌ No models found!")
        print("Install models with: ollama pull <model-name>")
        print("Example: ollama pull tinyllama:1.1b")
        exit(1)
    
    selected_models, skipped_models = filter_models(installed_models)

    if skipped_models:
        print("\n⏭️  Skipping models by built-in filter:")
        for model in skipped_models:
            print(f"   - {model}")
        print(f"   Patterns: {', '.join(SKIP_MODEL_PATTERNS)}")

    if not selected_models:
        print("\n❌ No models left to benchmark after applying skip filters.")
        print("Installed models:")
        for model in installed_models:
            print(f"   - {model}")
        exit(1)

    print(f"\n📋 Found {len(selected_models)} model(s) to benchmark:")
    for model in selected_models:
        print(f"   - {model}")
    print()
    
    # Run benchmarks
    results = []
    for model in selected_models:
        result = benchmark_model(model, prompt_suite)
        results.append(result)
        
        # Explicitly unload model to free memory before next test
        unload_model(model)
        
        time.sleep(MODEL_COOLDOWN_SEC)  # Cool down between tests
    
    # Generate report
    report = generate_report(results, prompt_suite)
    
    # Save report
    report_path = Path(__file__).parent / f"benchmark_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text(report, encoding='utf-8')
    
    print(f"\n✅ Benchmark complete!")
    print(f"📄 Report saved: {report_path}")
    print("\n" + report)

if __name__ == "__main__":
    main()
