
import os
import time
import logging
import threading
import uuid
import shutil
import json
from pathlib import Path
from datetime import datetime, timezone
import requests
from typing import Any, Iterator

# Connects to the OLLAMA API (running locally on your machine, accessed via host.docker.internal from Docker)
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://host.docker.internal:11434/api/generate")

# Model name from environment variable (defaults to qwen2.5:3b if not set)
MODEL_NAME = os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:3b")
NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "1024"))
TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.0"))
CONNECT_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_CONNECT_TIMEOUT_SECONDS", "10"))
READ_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_READ_TIMEOUT_SECONDS", "180"))
WARMUP_ENABLED = os.getenv("OLLAMA_WARMUP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
WARMUP_PROMPT = os.getenv("OLLAMA_WARMUP_PROMPT", "Reply with exactly: OK")
WARMUP_READ_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_WARMUP_READ_TIMEOUT_SECONDS", "120"))
WARMUP_RETRY_INTERVAL_SECONDS = float(os.getenv("OLLAMA_WARMUP_RETRY_INTERVAL_SECONDS", "300"))

logger = logging.getLogger("uvicorn.error")
_warmup_lock = threading.Lock()
_warmup_completed = False
_warmup_last_attempt_monotonic = 0.0
_warmup_last_status = "never_attempted"


def _resolve_prompts_dir() -> Path:
    prompts_dir = Path("/data/responses")
    if not Path("/data").exists():
        return (Path(__file__).resolve().parents[2] / "data" / "responses").resolve()
    return prompts_dir.resolve()


def ensure_prompt_directories() -> tuple[Path, Path]:
    prompts_dir = _resolve_prompts_dir()
    archive_dir = prompts_dir / "archive"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    return prompts_dir, archive_dir


def _write_prompt_record(record: dict[str, Any], prefix: str = "llm") -> dict[str, Any]:
    prompts_dir, _ = ensure_prompt_directories()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_id = f"{prefix}_{timestamp}_{uuid.uuid4().hex[:8]}"
    record_path = (prompts_dir / f"{record_id}.json").resolve()
    record["record_id"] = record_id
    record["record_file"] = str(record_path)
    record["created_at_utc"] = datetime.now(timezone.utc).isoformat()

    with record_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=True)

    return {
        "record_id": record_id,
        "record_file": str(record_path),
    }


def _attach_prompt_record(
    result: dict[str, Any],
    prompt: str,
    payload: dict[str, Any],
    status: str,
    llm_response: Any,
    error_payload: Any,
) -> dict[str, Any]:
    sanitized_response = llm_response
    if isinstance(llm_response, dict):
        sanitized_response = {key: value for key, value in llm_response.items() if key != "context"}

    try:
        result.update(
            _write_prompt_record(
                {
                    "type": "llm_call",
                    "status": status,
                    "model": MODEL_NAME,
                    "api_url": OLLAMA_API_URL,
                    "prompt": prompt,
                    "input": payload,
                    "llm_response": sanitized_response,
                    "error": error_payload,
                }
            )
        )
    except Exception:
        logger.exception("Failed to persist prompt record")
    return result

def save_combined_response_record(
    prepared_data: dict[str, Any],
    llm_response: str,
) -> dict[str, Any]:
    """
    Saves a slim combined analysis record to data/responses as llm_*.json.
    Only persists prompt/response and minimal config targeting metadata needed by
    response processing workflows.
    """
    try:
        prompts_dir, _ = ensure_prompt_directories()
        input_data = prepared_data.get("input") if isinstance(prepared_data.get("input"), dict) else {}
        config_file_name = input_data.get("config_file_name")
        config_file_path = input_data.get("config_file_path")
        
        combined_record = {
            "type": "completed_analysis",
            "status": "completed",
            "prompt": prepared_data.get("prompt", ""),
            "llm_response": llm_response,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        if isinstance(config_file_name, str) and config_file_name.strip():
            combined_record["config_file_name"] = config_file_name.strip()
        if isinstance(config_file_path, str) and config_file_path.strip():
            combined_record["config_file_path"] = config_file_path.strip()

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        record_id = f"llm_{timestamp}_{uuid.uuid4().hex[:8]}"
        combined_path = (prompts_dir / f"{record_id}.json").resolve()
        
        combined_record["record_id"] = record_id
        combined_record["record_file"] = str(combined_path)
        
        with combined_path.open("w", encoding="utf-8") as f:
            json.dump(combined_record, f, indent=2, ensure_ascii=True)

        return {
            "saved": True,
            "combined_record_file": str(combined_path),
            "record_id": record_id,
        }
    except Exception:
        logger.exception("Failed to save combined response record")
        return {
            "saved": False,
            "reason": "save_exception",
        }


def archive_prompt_record(record_file: str | Path) -> dict[str, Any]:
    prompts_dir, archive_dir = ensure_prompt_directories()
    source_path = Path(record_file)
    if not source_path.is_absolute():
        source_path = (prompts_dir / source_path).resolve()
    else:
        source_path = source_path.resolve()

    if not source_path.exists():
        return {
            "archived": False,
            "reason": "not_found",
            "source": str(source_path),
        }

    if source_path.parent == archive_dir:
        return {
            "archived": True,
            "already_archived": True,
            "source": str(source_path),
            "archived_file": str(source_path),
        }

    if source_path.parent != prompts_dir:
        return {
            "archived": False,
            "reason": "outside_responses_directory",
            "source": str(source_path),
        }

    target_path = (archive_dir / source_path.name).resolve()
    if target_path.exists():
        target_path = (archive_dir / f"{source_path.stem}_{uuid.uuid4().hex[:8]}{source_path.suffix}").resolve()

    shutil.move(str(source_path), str(target_path))
    return {
        "archived": True,
        "source": str(source_path),
        "archived_file": str(target_path),
    }


def _build_payload(prompt: str) -> dict:
    """Build standard Ollama payload with deterministic generation settings."""
    return {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": NUM_PREDICT,
        },
    }


def warmup_llm(force: bool = False) -> dict:
    """
    Run a small one-time warmup prompt to reduce first real request latency.
    Returns status details for logging/diagnostics.
    """
    global _warmup_completed
    global _warmup_last_attempt_monotonic
    global _warmup_last_status

    if not WARMUP_ENABLED:
        return {"status": "disabled"}

    if _warmup_completed and not force:
        return {"status": "already_warm"}

    # Avoid repeatedly blocking requests when warmup has recently failed.
    if not force and _warmup_last_attempt_monotonic:
        elapsed_since_last = time.monotonic() - _warmup_last_attempt_monotonic
        if elapsed_since_last < WARMUP_RETRY_INTERVAL_SECONDS:
            return {
                "status": "warmup_cooldown",
                "retry_in_sec": round(WARMUP_RETRY_INTERVAL_SECONDS - elapsed_since_last, 2),
                "last_status": _warmup_last_status,
            }

    with _warmup_lock:
        if _warmup_completed and not force:
            return {"status": "already_warm"}

        if not force and _warmup_last_attempt_monotonic:
            elapsed_since_last = time.monotonic() - _warmup_last_attempt_monotonic
            if elapsed_since_last < WARMUP_RETRY_INTERVAL_SECONDS:
                return {
                    "status": "warmup_cooldown",
                    "retry_in_sec": round(WARMUP_RETRY_INTERVAL_SECONDS - elapsed_since_last, 2),
                    "last_status": _warmup_last_status,
                }

        _warmup_last_attempt_monotonic = time.monotonic()

        started = time.monotonic()
        try:
            payload = _build_payload(WARMUP_PROMPT)
            # Keep warmup short: tiny response and deterministic output.
            payload["options"]["num_predict"] = 8
            response = requests.post(
                f"{OLLAMA_API_URL}",
                json=payload,
                timeout=(CONNECT_TIMEOUT_SECONDS, WARMUP_READ_TIMEOUT_SECONDS),
            )

            elapsed = round(time.monotonic() - started, 2)
            if response.status_code == 200:
                _warmup_completed = True
                _warmup_last_status = "warmed"
                logger.info("Ollama warmup completed in %ss (model=%s)", elapsed, MODEL_NAME)
                return {"status": "warmed", "elapsed_sec": elapsed}

            _warmup_last_status = "warmup_http_error"
            logger.warning(
                "Ollama warmup returned HTTP %s in %ss (model=%s)",
                response.status_code,
                elapsed,
                MODEL_NAME,
            )
            return {
                "status": "warmup_http_error",
                "elapsed_sec": elapsed,
                "http_status": response.status_code,
            }
        except requests.exceptions.RequestException as e:
            elapsed = round(time.monotonic() - started, 2)
            _warmup_last_status = "warmup_request_error"
            logger.warning("Ollama warmup request failed after %ss: %s", elapsed, str(e))
            return {
                "status": "warmup_request_error",
                "elapsed_sec": elapsed,
                "error": str(e),
            }


def _warmup_llm_async_if_needed() -> None:
    """Run warmup in a background thread so regular requests are not blocked."""
    if not WARMUP_ENABLED:
        return
    if _warmup_completed:
        return
    if _warmup_last_attempt_monotonic:
        elapsed_since_last = time.monotonic() - _warmup_last_attempt_monotonic
        if elapsed_since_last < WARMUP_RETRY_INTERVAL_SECONDS:
            return

    thread = threading.Thread(target=warmup_llm, kwargs={"force": False}, daemon=True)
    thread.start()

# Defines LLM prompt constraints.
def query_llm(
    prompt: str,
    allow_retry: bool = False,
    max_retries: int = 0,
    backoff_initial_seconds: float = 2.0,
    backoff_multiplier: float = 2.0,
    backoff_max_seconds: float = 30.0,
) -> dict[str, Any]:
    """
    Query the Ollama LLM model.
    Returns a dict with either 'response' or 'error' key.
    """
    # Lazy warmup fallback in case startup warmup did not run.
    _warmup_llm_async_if_needed()
    payload = _build_payload(prompt)

    retries_allowed = max(0, int(max_retries)) if allow_retry else 0
    attempt = 0
    delay_seconds = max(0.0, float(backoff_initial_seconds))
    total_attempts = retries_allowed + 1

    while attempt < total_attempts:
        attempt += 1
        started = time.monotonic()
        logger.info(
            "Sending Ollama request (attempt=%s/%s, model=%s, url=%s, connect_timeout=%ss, read_timeout=%ss)",
            attempt,
            total_attempts,
            MODEL_NAME,
            OLLAMA_API_URL,
            CONNECT_TIMEOUT_SECONDS,
            READ_TIMEOUT_SECONDS,
        )

        try:
            response = requests.post(
                f"{OLLAMA_API_URL}",
                json=payload,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )

            if response.status_code != 200:
                elapsed = round(time.monotonic() - started, 2)
                retryable = response.status_code >= 500
                logger.error(
                    "Ollama API returned HTTP %s after %ss (attempt=%s/%s)",
                    response.status_code,
                    elapsed,
                    attempt,
                    total_attempts,
                )
                if retryable and attempt < total_attempts:
                    logger.warning("Retrying after HTTP %s in %ss", response.status_code, delay_seconds)
                    time.sleep(delay_seconds)
                    delay_seconds = min(max(0.0, float(backoff_max_seconds)), delay_seconds * max(1.0, float(backoff_multiplier)))
                    continue
                result = {
                    "error": f"Ollama API error (HTTP {response.status_code}): {response.text}",
                    "error_type": "http_error",
                    "retryable": retryable,
                    "attempts": attempt,
                }
                return _attach_prompt_record(result, prompt, payload, "error", None, dict(result))

            data = response.json()
            if "response" in data:
                elapsed = round(time.monotonic() - started, 2)
                logger.info("Ollama response received successfully in %ss (attempt=%s/%s)", elapsed, attempt, total_attempts)
                result = {
                    "response": data["response"],
                    "attempts": attempt,
                }
                return _attach_prompt_record(result, prompt, payload, "ok", data, None)

            elapsed = round(time.monotonic() - started, 2)
            logger.error("Unexpected Ollama response format after %ss: %s", elapsed, data)
            result = {
                "error": f"Unexpected response format from Ollama: {data}",
                "error_type": "bad_response",
                "retryable": False,
                "attempts": attempt,
            }
            return _attach_prompt_record(result, prompt, payload, "error", data, dict(result))

        except requests.exceptions.Timeout:
            elapsed = round(time.monotonic() - started, 2)
            logger.error("Ollama request timed out after %ss (attempt=%s/%s)", elapsed, attempt, total_attempts)
            if attempt < total_attempts:
                logger.warning("Retrying timeout in %ss", delay_seconds)
                time.sleep(delay_seconds)
                delay_seconds = min(max(0.0, float(backoff_max_seconds)), delay_seconds * max(1.0, float(backoff_multiplier)))
                continue
            result = {
                "error": (
                    "Ollama request timed out while generating a response. "
                    "If this is the first run, model warm-up can be slow. "
                    "Otherwise, try a smaller model or increase OLLAMA_READ_TIMEOUT_SECONDS."
                ),
                "error_type": "timeout",
                "retryable": True,
                "attempts": attempt,
            }
            return _attach_prompt_record(result, prompt, payload, "error", None, dict(result))
        except requests.exceptions.ConnectionError as e:
            logger.error("Cannot connect to Ollama service: %s", e)
            if attempt < total_attempts:
                logger.warning("Retrying connection error in %ss", delay_seconds)
                time.sleep(delay_seconds)
                delay_seconds = min(max(0.0, float(backoff_max_seconds)), delay_seconds * max(1.0, float(backoff_multiplier)))
                continue
            result = {
                "error": (
                    "Cannot connect to Ollama service. Ensure Ollama is running locally on "
                    "http://localhost:11434 (try: ollama serve)."
                ),
                "error_type": "connection_error",
                "retryable": True,
                "attempts": attempt,
            }
            return _attach_prompt_record(result, prompt, payload, "error", None, dict(result))
        except requests.exceptions.RequestException as e:
            logger.error("Network error communicating with Ollama: %s", e)
            if attempt < total_attempts:
                logger.warning("Retrying network error in %ss", delay_seconds)
                time.sleep(delay_seconds)
                delay_seconds = min(max(0.0, float(backoff_max_seconds)), delay_seconds * max(1.0, float(backoff_multiplier)))
                continue
            result = {
                "error": f"Network error communicating with Ollama: {str(e)}",
                "error_type": "network_error",
                "retryable": True,
                "attempts": attempt,
            }
            return _attach_prompt_record(result, prompt, payload, "error", None, dict(result))
        except Exception as e:
            logger.exception("Unexpected error while querying Ollama")
            result = {
                "error": f"Unexpected error: {str(e)}",
                "error_type": "unexpected_error",
                "retryable": False,
                "attempts": attempt,
            }
            return _attach_prompt_record(result, prompt, payload, "error", None, dict(result))

    result = {
        "error": "Unexpected retry loop termination while querying Ollama.",
        "error_type": "unexpected_error",
        "retryable": False,
        "attempts": attempt,
    }
    return _attach_prompt_record(result, prompt, payload, "error", None, dict(result))


def query_llm_stream(prompt: str) -> Iterator[dict[str, Any]]:
    """
    Stream response chunks from Ollama.
    Yields dicts with one of:
      {"type": "chunk",  "content": "..."}
      {"type": "done",   "full_response": "...", "elapsed_sec": n, "attempts": 1}
      {"type": "error",  "error": "...", "error_type": "...", "retryable": bool, "attempts": 1}
    """
    _warmup_llm_async_if_needed()
    stream_payload = _build_payload(prompt)
    stream_payload["stream"] = True
    started = time.monotonic()
    accumulated: list[str] = []

    try:
        logger.info(
            "Sending streamed Ollama request (model=%s, url=%s, connect_timeout=%ss, read_timeout=%ss)",
            MODEL_NAME, OLLAMA_API_URL, CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS,
        )
        with requests.post(
            OLLAMA_API_URL,
            json=stream_payload,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            stream=True,
        ) as response:
            if response.status_code != 200:
                elapsed = round(time.monotonic() - started, 2)
                logger.error("Streamed Ollama API returned HTTP %s after %ss", response.status_code, elapsed)
                yield {
                    "type": "error",
                    "error": f"Ollama API error (HTTP {response.status_code}): {response.text}",
                    "error_type": "http_error",
                    "retryable": response.status_code >= 500,
                    "attempts": 1,
                }
                return

            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.warning("Skipping non-JSON Ollama stream line: %s", raw_line)
                    continue

                if data.get("error"):
                    logger.error("Ollama stream returned error payload: %s", data["error"])
                    yield {
                        "type": "error",
                        "error": str(data["error"]),
                        "error_type": "stream_error",
                        "retryable": False,
                        "attempts": 1,
                    }
                    return

                chunk = str(data.get("response", ""))
                if chunk:
                    accumulated.append(chunk)
                    yield {"type": "chunk", "content": chunk}

                if data.get("done"):
                    elapsed = round(time.monotonic() - started, 2)
                    logger.info("Streamed Ollama response completed in %ss", elapsed)
                    yield {
                        "type": "done",
                        "full_response": "".join(accumulated),
                        "elapsed_sec": elapsed,
                        "attempts": 1,
                    }
                    return

    except requests.exceptions.Timeout:
        elapsed = round(time.monotonic() - started, 2)
        logger.error("Streamed Ollama request timed out after %ss", elapsed)
        yield {
            "type": "error",
            "error": (
                "Ollama request timed out. "
                "Try a smaller model or increase OLLAMA_READ_TIMEOUT_SECONDS."
            ),
            "error_type": "timeout",
            "retryable": True,
            "attempts": 1,
        }
    except requests.exceptions.ConnectionError as exc:
        logger.error("Cannot connect to Ollama during stream: %s", exc)
        yield {
            "type": "error",
            "error": "Cannot connect to Ollama. Ensure Ollama is running (ollama serve).",
            "error_type": "connection_error",
            "retryable": True,
            "attempts": 1,
        }
    except requests.exceptions.RequestException as exc:
        logger.error("Network error during Ollama stream: %s", exc)
        yield {
            "type": "error",
            "error": f"Network error communicating with Ollama: {str(exc)}",
            "error_type": "network_error",
            "retryable": True,
            "attempts": 1,
        }
    except Exception as exc:
        logger.exception("Unexpected error during Ollama stream")
        yield {
            "type": "error",
            "error": f"Unexpected error: {str(exc)}",
            "error_type": "unexpected_error",
            "retryable": False,
            "attempts": 1,
        }
