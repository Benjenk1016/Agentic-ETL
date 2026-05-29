import json
from pathlib import Path

import pandas as pd
import pytest

from backend.src.column_extract import extract_columns


# Tests that extract_columns returns a JSON payload containing ordered column names and positions.
# Calls extract_columns.
def test_extract_columns_returns_llm_payload_json(tmp_path: Path) -> None:
    excel_path = tmp_path / "sample.xlsx"
    pd.DataFrame({"name": ["a"], "age": [1], "city": ["x"]}).to_excel(
        excel_path,
        index=False,
    )

    payload = extract_columns(excel_path)
    result = json.loads(payload)

    sheet = result["sheet 1 (Sheet1)"]
    assert sheet["column_names"] == ["name", "age", "city"]
    assert sheet["column_positions"] == [["A", 1], ["B", 1], ["C", 1]]
    assert sheet["column_count"] == 3
    assert sheet["header_row"] == 1
    assert sheet["data_start_row"] == 2
    assert sheet["columns"][0]["cell_ref"] == "A1"
    assert sheet["columns"][0]["detected_type"] == "string"


# Tests that extract_columns returns the first non-empty cell from each column.
# Calls extract_columns.
def test_extract_columns_first_non_empty_cell_per_column(tmp_path: Path) -> None:
    excel_path = tmp_path / "first_non_empty.xlsx"
    pd.DataFrame(
        [
            [None, "age", None],
            [None, 1, None],
            ["name", 2, "city"],
        ]
    ).to_excel(
        excel_path,
        index=False,
        header=False,
    )

    payload = extract_columns(excel_path)
    result = json.loads(payload)

    sheet = result["sheet 1 (Sheet1)"]
    assert sheet["column_names"] == ["name", "age", "city"]
    assert sheet["column_positions"] == [["A", 3], ["B", 1], ["C", 3]]
    assert sheet["column_count"] == 3
    assert sheet["header_row"] == 1
    assert sheet["data_start_row"] == 2
    assert sheet["columns"][0]["cell_ref"] == "A3"
    assert sheet["columns"][0]["detected_type"] == "string"


# Tests that extract_columns scans all sheets and tracks which sheet each column header came from.
# Calls extract_columns.
def test_extract_columns_across_multiple_sheets_tracks_sheet_name(tmp_path: Path) -> None:
    excel_path = tmp_path / "multiple_sheets.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        pd.DataFrame(
            [
                ["id", "Unnamed: 1"],
                [1, "full_name"],
            ]
        ).to_excel(writer, sheet_name="Customers", index=False, header=False)
        pd.DataFrame(
            [
                ["order_id", "amount"],
                [1001, 25.5],
            ]
        ).to_excel(writer, sheet_name="Orders", index=False, header=False)

    payload = extract_columns(excel_path)
    result = json.loads(payload)

    assert result["sheet 1 (Customers)"]["column_names"] == ["id", "full_name"]
    assert result["sheet 1 (Customers)"]["column_positions"] == [["A", 1], ["B", 2]]
    assert result["sheet 2 (Orders)"]["column_names"] == ["order_id", "amount"]
    assert result["sheet 2 (Orders)"]["column_positions"] == [["A", 1], ["B", 1]]
    assert result["sheet 1 (Customers)"]["column_count"] == 2
    assert result["sheet 2 (Orders)"]["column_count"] == 2


# Tests that extract_columns raises FileNotFoundError when the input file does not exist.
# Calls extract_columns.
def test_extract_columns_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError, match="Input file not found"):
        extract_columns("does_not_exist.xlsx")


# Tests that extract_columns rejects unsupported file extensions with ValueError.
# Calls extract_columns.
def test_extract_columns_unsupported_file_type_raises(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_columns(csv_path)
