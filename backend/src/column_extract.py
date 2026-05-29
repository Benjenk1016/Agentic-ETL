import json
import sys
from collections import Counter
from pathlib import Path
import pandas as pd


# Header values matching these rules are skipped while scanning each column.
# example list of words : "unnamed", "empty", "blank", "null"
SKIP_HEADER_PREFIXES = {
    "unnamed", "ben"
}
SKIP_HEADER_EXACT = set()


# added functionality to detect column headers by iterating through each column
# to find the first non-empty cell and position
# Function does return non-column header types, such as int.
# But LLM will interpret them once we pass them to it


# Extracts column names from an Excel file and returns them in a JSON payload.
# JSON payload is grouped by sheet (for example: "sheet 1 (Sheet1)").
def extract_columns(input_file_path) -> str:
    path = Path(input_file_path)

    # validate file exists and is an Excel file
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    if path.suffix.lower() not in {".xlsx", ".xls", ".xlsm"}:
        raise ValueError(f"Unsupported file type: {path.suffix}. Please provide an Excel file.")

    # Read all sheets without headers so we can scan each column for a real header.
    sheet_data = pd.read_excel(path, header=None, sheet_name=None)

    # If no sheets are present, return an empty payload.
    if not sheet_data:
        return json.dumps({}, ensure_ascii=False)

    # Helper function to convert zero-based column index to Excel column letter.
    def _excel_column_letter(column_number: int) -> str:
        letters = ""
        while column_number > 0:
            column_number, remainder = divmod(column_number - 1, 26)
            letters = chr(65 + remainder) + letters
        return letters

    # Helper function to determine if a cell value is non-empty.
    def _is_non_empty(value) -> bool:
        if pd.isna(value):
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return True

    def _detect_value_type(value) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "numeric"
        if hasattr(value, "year") and hasattr(value, "month"):
            return "datetime"
        return "string"

    normalized_skip_prefixes = {
        prefix.strip().lower()
        for prefix in SKIP_HEADER_PREFIXES
        if isinstance(prefix, str) and prefix.strip()
    }
    normalized_skip_exact = {
        exact.strip().lower()
        for exact in SKIP_HEADER_EXACT
        if isinstance(exact, str) and exact.strip()
    }

    # Pandas often labels empty Excel headers as values like "Unnamed: 3".
    def _is_unnamed_header(value: str) -> bool:
        normalized_value = value.strip().lower()
        if not normalized_value:
            return False
        if normalized_value in normalized_skip_exact:
            return True
        return any(normalized_value.startswith(prefix) for prefix in normalized_skip_prefixes)

    payload = {}

    # Iterate through each sheet, then each column, to find the first non-empty usable header and its position.
    for sheet_index, (sheet_name, df_raw) in enumerate(sheet_data.items(), start=1):
        sheet_column_names = []
        sheet_column_positions = []
        sheet_column_details = []

        if df_raw.empty:
            sheet_key = f"sheet {sheet_index} ({sheet_name})"
            payload[sheet_key] = {
                "column_names": sheet_column_names,
                "column_positions": sheet_column_positions,
                "columns": sheet_column_details,
                "column_count": 0,
                "header_row": None,
                "data_start_row": None,
            }
            continue

        for zero_based_column_index in range(df_raw.shape[1]):
            column_series = df_raw.iloc[:, zero_based_column_index]

            # Find the first non-empty, non-placeholder header in the column.
            first_value = None
            first_row_index = None
            use_this_marker_row = None

            for zero_based_row_index, value in column_series.items():
                if _is_non_empty(value):
                    cleaned_candidate = value.strip() if isinstance(value, str) else str(value)
                    if _is_unnamed_header(cleaned_candidate):
                        continue
                    # Check if this is a "USE THIS" marker — skip it and continue to next row
                    if cleaned_candidate.upper() == "USE THIS":
                        use_this_marker_row = zero_based_row_index
                        continue
                    first_value = value
                    first_row_index = zero_based_row_index
                    break

            # If no usable header is found in the column, skip it.
            if first_row_index is None:
                continue

            # Convert zero-based indices to one-based for Excel format.
            one_based_column_index = zero_based_column_index + 1
            one_based_row_index = first_row_index + 1
            cleaned_value = first_value.strip() if isinstance(first_value, str) else str(first_value)
            column_letter = _excel_column_letter(one_based_column_index)
            cell_ref = f"{column_letter}{one_based_row_index}"

            sheet_column_names.append(cleaned_value)
            sheet_column_positions.append([column_letter, one_based_row_index])
            column_detail = {
                "index": zero_based_column_index,
                "column_number": one_based_column_index,
                "name": cleaned_value,
                "cell_ref": cell_ref,
                "header_row": one_based_row_index,
                "detected_type": _detect_value_type(first_value),
            }
            # Flag if this column had a "USE THIS" marker above it
            if use_this_marker_row is not None:
                column_detail["use_this_marker_row"] = use_this_marker_row + 1  # Convert to one-based
                column_detail["marked_with_use_this"] = True
            sheet_column_details.append(column_detail)

        duplicate_counts = Counter(
            str(detail["name"]).strip().lower()
            for detail in sheet_column_details
            if str(detail["name"]).strip()
        )
        duplicate_tracker: dict[str, int] = {}
        for detail in sheet_column_details:
            normalized_name = str(detail["name"]).strip().lower()
            if duplicate_counts.get(normalized_name, 0) > 1:
                duplicate_tracker[normalized_name] = duplicate_tracker.get(normalized_name, 0) + 1
                detail["is_duplicate"] = True
                detail["duplicate_position"] = duplicate_tracker[normalized_name]
            else:
                detail["is_duplicate"] = False
                detail["duplicate_position"] = None

        header_rows = sorted({detail["header_row"] for detail in sheet_column_details})
        sheet_header_row = header_rows[0] if len(header_rows) == 1 else (min(header_rows) if header_rows else None)
        data_start_row = sheet_header_row + 1 if sheet_header_row is not None else None

        sheet_key = f"sheet {sheet_index} ({sheet_name})"
        payload[sheet_key] = {
            "column_names": sheet_column_names,
            "column_positions": sheet_column_positions,
            "columns": sheet_column_details,
            "column_count": len(sheet_column_details),
            "header_row": sheet_header_row,
            "data_start_row": data_start_row,
        }

    # Writes per-sheet column names and positions to a JSON payload for LLM consumption.
    return json.dumps(payload, ensure_ascii=False)


# Extracts actual data rows from an Excel file for use in the synced config CSV download.
# Returns a JSON payload grouped by sheet. Each sheet contains:
#   "headers": list of column name strings
#   "rows": list of row value lists (data rows below the header row)
def extract_data_rows(input_file_path) -> str:
    path = Path(input_file_path)

    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    if path.suffix.lower() not in {".xlsx", ".xls", ".xlsm"}:
        raise ValueError(f"Unsupported file type: {path.suffix}. Please provide an Excel file.")

    sheet_data_raw = pd.read_excel(path, header=None, sheet_name=None)

    if not sheet_data_raw:
        return json.dumps({}, ensure_ascii=False)

    normalized_skip_prefixes = {
        prefix.strip().lower()
        for prefix in SKIP_HEADER_PREFIXES
        if isinstance(prefix, str) and prefix.strip()
    }

    def _is_skip_candidate(value) -> bool:
        if pd.isna(value):
            return True
        text = value.strip() if isinstance(value, str) else str(value)
        if not text:
            return True
        norm = text.lower()
        return any(norm.startswith(p) for p in normalized_skip_prefixes)

    payload = {}

    for sheet_index, (sheet_name, df_raw) in enumerate(sheet_data_raw.items(), start=1):
        sheet_key = f"sheet {sheet_index} ({sheet_name})"

        if df_raw.empty:
            payload[sheet_key] = {"headers": [], "rows": []}
            continue

        # Find the header row for each column (same logic as extract_columns).
        header_row_votes: list[int] = []
        for col_idx in range(df_raw.shape[1]):
            col = df_raw.iloc[:, col_idx]
            for row_idx, val in col.items():
                if not _is_skip_candidate(val):
                    header_row_votes.append(int(row_idx))
                    break

        if not header_row_votes:
            payload[sheet_key] = {"headers": [], "rows": []}
            continue

        # Use the most common detected header row across all columns.
        header_row = Counter(header_row_votes).most_common(1)[0][0]

        # Re-read the sheet using the detected header row.
        df = pd.read_excel(path, sheet_name=sheet_name, header=header_row)

        # Collect clean column headers.
        headers = [str(col).strip() for col in df.columns]

        # Serialize each data row, converting NaN and non-string types to safe values.
        rows: list[list] = []
        for _, row in df.iterrows():
            row_values = []
            for val in row:
                if pd.isna(val):
                    row_values.append("")
                elif isinstance(val, (int, float, bool)):
                    row_values.append(val)
                else:
                    row_values.append(str(val))
            rows.append(row_values)

        payload[sheet_key] = {"headers": headers, "rows": rows}

    return json.dumps(payload, ensure_ascii=False, default=str)


# function to open file dialog and return selected file path

