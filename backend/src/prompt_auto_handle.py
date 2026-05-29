import argparse
import csv
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _resolve_config_candidates() -> list[Path]:
    config_dir = Path("/data/config")
    if not Path("/data").exists():
        config_dir = Path(__file__).resolve().parents[2] / "data" / "config"
    if not config_dir.exists():
        return []
    return sorted(
        [path for path in config_dir.iterdir() if path.is_file()],
        key=lambda path: path.name.lower(),
    )


def _resolve_config_target(record: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    input_data = record.get("input") if isinstance(record.get("input"), dict) else {}
    config_file_path = record.get("config_file_path") or input_data.get("config_file_path")
    config_file_name = (
        record.get("config_file_name")
        or input_data.get("config_file_name")
        or input_data.get("selected_config")
    )

    if isinstance(config_file_path, str) and config_file_path.strip():
        candidate = Path(config_file_path).expanduser()
        if candidate.exists():
            return candidate.resolve(), {
                "config_file_name": candidate.name,
                "config_file_path": str(candidate.resolve()),
                "resolution": "explicit_path",
            }

    candidates = _resolve_config_candidates()
    if config_file_name:
        for candidate in candidates:
            if candidate.name == config_file_name or candidate.name.lower() == str(config_file_name).lower():
                return candidate.resolve(), {
                    "config_file_name": candidate.name,
                    "config_file_path": str(candidate.resolve()),
                    "resolution": "matched_name",
                }

    if len(candidates) == 1:
        candidate = candidates[0].resolve()
        return candidate, {
            "config_file_name": candidate.name,
            "config_file_path": str(candidate),
            "resolution": "single_candidate",
        }

    return None, {
        "config_file_name": config_file_name,
        "config_file_path": config_file_path,
        "resolution": "ambiguous_or_missing",
        "available_config_files": [path.name for path in candidates],
    }


def _extract_response_text(record: dict[str, Any]) -> str:
    llm_response = record.get("llm_response")
    if isinstance(llm_response, dict):
        text = llm_response.get("response")
        if isinstance(text, str):
            return text
    if isinstance(llm_response, str):
        return llm_response
    return ""


def _csv_cell(value: Any) -> str:
    return '"' + str("" if value is None else value).replace('"', '""') + '"'


def _csv_row(row: list[Any]) -> str:
    return ",".join(_csv_cell(value) for value in row)


def _normalize_words(text: str) -> list[str]:
    return [word for word in text.replace("-", " ").replace("_", " ").split() if len(word) >= 2]


def _column_similarity(left: str, right: str) -> float:
    left_text = str(left or "").lower().replace(" ", "").replace("_", "").replace("-", "")
    right_text = str(right or "").lower().replace(" ", "").replace("_", "").replace("-", "")
    if left_text == right_text:
        return 1.0
    if left_text and right_text and left_text in right_text:
        return len(left_text) / len(right_text)
    if left_text and right_text and right_text in left_text:
        return len(right_text) / len(left_text)

    left_words = set(_normalize_words(str(left or "").lower()))
    right_words = set(_normalize_words(str(right or "").lower()))
    if not left_words or not right_words:
        return 0.0
    shared = len(left_words & right_words)
    return shared / max(len(left_words), len(right_words))


def _build_column_map_greedy(config_headers: list[str], input_headers: list[str]) -> list[int]:
    min_score = 0.35
    pairs: list[tuple[int, int, float]] = []
    for config_index, config_header in enumerate(config_headers):
        for input_index, input_header in enumerate(input_headers):
            score = _column_similarity(config_header, input_header)
            if score >= min_score:
                pairs.append((config_index, input_index, score))

    pairs.sort(key=lambda item: item[2], reverse=True)

    column_map = [-1] * len(config_headers)
    used_input_indexes: set[int] = set()
    for config_index, input_index, _score in pairs:
        if column_map[config_index] == -1 and input_index not in used_input_indexes:
            column_map[config_index] = input_index
            used_input_indexes.add(input_index)

    return column_map


def _sheet_base_name(key: str) -> str:
    text = str(key or "")
    if "(" in text and ")" in text:
        return text[text.find("(") + 1 : text.rfind(")")].lower()
    return text.lower()


def _build_sheet_map_greedy(config_sheet_entries: list[tuple[str, dict[str, Any]]], input_row_data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    input_keys = list(input_row_data.keys())
    pairs: list[tuple[int, int, float]] = []
    for config_index, (config_sheet_key, config_sheet_data) in enumerate(config_sheet_entries):
        config_headers = (config_sheet_data or {}).get("column_names", [])
        for input_index, input_key in enumerate(input_keys):
            input_sheet_data = input_row_data[input_key]
            score = 1000 if _sheet_base_name(config_sheet_key) == _sheet_base_name(input_key) else sum(
                1
                for config_header in config_headers
                if any(_column_similarity(config_header, input_header) >= 0.5 for input_header in (input_sheet_data or {}).get("headers", []))
            )
            pairs.append((config_index, input_index, float(score)))

    pairs.sort(key=lambda item: item[2], reverse=True)

    sheet_map: dict[int, dict[str, Any]] = {}
    used_input_indexes: set[int] = set()
    for config_index, input_index, _score in pairs:
        if config_index not in sheet_map and input_index not in used_input_indexes:
            sheet_map[config_index] = input_row_data[input_keys[input_index]]
            used_input_indexes.add(input_index)

    return sheet_map


def _build_synced_config_csv(
    input_column_data: dict[str, Any] | None,
    input_row_data: dict[str, Any] | None,
    config_column_data: dict[str, Any] | None,
) -> str:
    if (
        config_column_data and isinstance(config_column_data, dict) and config_column_data and
        input_row_data and isinstance(input_row_data, dict) and input_row_data
    ):
        config_sheet_entries = list(config_column_data.items())
        sheet_map = _build_sheet_map_greedy(config_sheet_entries, input_row_data)
        all_rows: list[list[Any]] = []

        for config_index, (_config_sheet_key, config_sheet_data) in enumerate(config_sheet_entries):
            config_headers = (config_sheet_data or {}).get("column_names", [])
            if not config_headers:
                continue

            matched_input_sheet = sheet_map.get(config_index)
            input_headers = (matched_input_sheet or {}).get("headers", [])
            input_rows = (matched_input_sheet or {}).get("rows", [])
            column_map = _build_column_map_greedy(config_headers, input_headers)
            mapped_indexes = {index for index in column_map if index >= 0}
            extra_input_columns = [
                (header, index)
                for index, header in enumerate(input_headers)
                if index not in mapped_indexes
            ]

            full_headers = list(config_headers) + [header for header, _index in extra_input_columns]
            if all_rows:
                all_rows.append([])
            all_rows.append(full_headers)

            for input_row in input_rows:
                mapped_values = [input_row[index] if index >= 0 and index < len(input_row) else "" for index in column_map]
                extra_values = [input_row[index] if index < len(input_row) else "" for _header, index in extra_input_columns]
                all_rows.append([*mapped_values, *extra_values])

        if all_rows:
            return "\n".join(_csv_row(row) for row in all_rows)

    if input_row_data and isinstance(input_row_data, dict) and input_row_data:
        all_rows = []
        for _sheet_key, sheet_data in input_row_data.items():
            headers = (sheet_data or {}).get("headers", [])
            data_rows = (sheet_data or {}).get("rows", [])
            if not headers:
                continue
            if all_rows:
                all_rows.append([])
            all_rows.append(headers)
            for row in data_rows:
                all_rows.append(row)
        if all_rows:
            return "\n".join(_csv_row(row) for row in all_rows)

    rows: list[list[Any]] = [["Sheet", "Column Name", "Column Position"]]
    if input_column_data and isinstance(input_column_data, dict):
        for sheet_key, sheet_data in input_column_data.items():
            column_names = (sheet_data or {}).get("column_names", [])
            column_positions = (sheet_data or {}).get("column_positions", [])
            for index, column_name in enumerate(column_names):
                position = column_positions[index] if index < len(column_positions) else ""
                position_text = "".join(map(str, position)) if isinstance(position, list) else str(position or "")
                rows.append([sheet_key, column_name, position_text])
    return "\n".join(_csv_row(row) for row in rows)


def _export_config_snapshot(config_path: Path) -> str:
    if config_path.suffix.lower() == ".csv":
        return config_path.read_text(encoding="utf-8")

    if config_path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        workbook = pd.read_excel(config_path, sheet_name=None, header=None)
        rows: list[list[Any]] = []
        for sheet_index, (sheet_name, dataframe) in enumerate(workbook.items(), start=1):
            if sheet_index > 1:
                rows.append([])
            rows.append([f"Sheet: {sheet_name}"])
            if dataframe.empty:
                continue
            for row in dataframe.itertuples(index=False):
                rows.append(list(row))
        return "\n".join(_csv_row(row) for row in rows)

    return config_path.read_text(encoding="utf-8")


def _build_result(record_path: Path, record: dict[str, Any], mode: str) -> dict[str, Any]:
    input_data = record.get("input") if isinstance(record.get("input"), dict) else {}
    input_file_name = input_data.get("input_file_name") or "(unknown input file)"
    resolved_config_path, config_resolution = _resolve_config_target(record)
    config_file_name = config_resolution.get("config_file_name") or input_data.get("config_file_name") or "(unknown config file)"
    response_text = _extract_response_text(record)
    input_column_data = input_data.get("input_column_data") if isinstance(input_data.get("input_column_data"), dict) else None
    input_row_data = input_data.get("input_row_data") if isinstance(input_data.get("input_row_data"), dict) else None
    config_column_data = input_data.get("config_column_data") if isinstance(input_data.get("config_column_data"), dict) else None

    proposed_changes = [
        f"Target config file: {config_file_name}",
        f"Source input file: {input_file_name}",
        f"Resolved config file path: {config_resolution.get('config_file_path') or '(unresolved)'}",
        "Read the saved response record and rebuild the config CSV from the persisted metadata.",
        "Use the exact config file from data/config when available.",
        "Return a downloadable CSV representation for review.",
    ]

    preview_lines: list[str] = []
    if response_text:
        for raw_line in response_text.splitlines():
            line = raw_line.strip()
            if "->" in line:
                preview_lines.append(line)
            if len(preview_lines) >= 10:
                break

    updated_config_csv = _build_synced_config_csv(input_column_data, input_row_data, config_column_data)
    download_name = f"{Path(config_file_name).stem if config_file_name else 'config'}_updated.csv"

    if not updated_config_csv and resolved_config_path and resolved_config_path.exists():
        updated_config_csv = _export_config_snapshot(resolved_config_path)
        download_name = f"{resolved_config_path.stem}_updated.csv"

    return {
        "status": "ok",
        "record_id": record.get("record_id") or record_path.stem,
        "mode": mode,
        "implemented": True,
        "requires_review": True,
        "resolved_config_file": str(resolved_config_path) if resolved_config_path else None,
        "config_resolution": config_resolution,
        "updated_config_file_name": download_name,
        "updated_config_csv": updated_config_csv,
        "download_ready": bool(updated_config_csv),
        "proposed_changes": proposed_changes,
        "mapping_line_preview": preview_lines,
        "script_message": (
            "Generated a downloadable config CSV from the saved response record. "
            "Review the result before archiving the response."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Response auto-handle transformer.")
    parser.add_argument("--record-file", required=True, help="Absolute path to response JSON record")
    parser.add_argument("--mode", default="apply", choices=["preview", "apply"], help="Execution mode")
    args = parser.parse_args()

    record_path = Path(args.record_file).resolve()
    if not record_path.exists() or not record_path.is_file():
        print(json.dumps({"status": "error", "message": f"Response file not found: {record_path}"}))
        return 1

    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"status": "error", "message": f"Failed to read response file: {exc}"}))
        return 1

    if not isinstance(record, dict):
        print(json.dumps({"status": "error", "message": "Response JSON must be an object."}))
        return 1

    result = _build_result(record_path, record, mode=args.mode)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
