import json
import pandas as pd
import pytest
import numpy as np

from pathlib import Path
from backend.src.column_extract import extract_columns

# Tests that extract_columns correctly handles an empty Excel file by returning empty column names and positions.
# Calls extract_columns.
def test_empty_Excel_file(tmp_path: Path):
    excel_path = tmp_path / "empty.xlsx"
    pd.DataFrame().to_excel(excel_path)

    payload = extract_columns(excel_path)

    result = json.loads(payload)
    assert result["sheet 1 (Sheet1)"]["column_names"] == []
    assert result["sheet 1 (Sheet1)"]["column_positions"] == []
    assert result["sheet 1 (Sheet1)"]["columns"] == []
    assert result["sheet 1 (Sheet1)"]["column_count"] == 0
    assert result["sheet 1 (Sheet1)"]["header_row"] is None
    assert result["sheet 1 (Sheet1)"]["data_start_row"] is None

#Tests that extract_columns skips columns that are blank to avoid including them in the output.
#Calls extract_columns.
def test_skip_blank_columns(tmp_path: Path):
    excel_path = tmp_path / "skipped_columns.xlsx"
    pd.DataFrame([
        [None, "age", None],
        [None, 1, None],
        [None, 2, None]
    ]).to_excel(excel_path, index=False, header=False)

    payload = extract_columns(excel_path)

    result = json.loads(payload)

    assert result["sheet 1 (Sheet1)"]["column_names"] == ["age"]
    assert result["sheet 1 (Sheet1)"]["column_positions"] == [["B", 1]]
    assert result["sheet 1 (Sheet1)"]["column_count"] == 1
    assert result["sheet 1 (Sheet1)"]["columns"][0]["cell_ref"] == "B1"

# Tests that extract_columns trims whitespace from column names for accurate and clean output.
# Calls extract_columns.
def test_extract_column_whitespace(tmp_path: Path):
    excel_path = tmp_path / "whitespace.xlsx"
    (
        pd.DataFrame({
        "name ": ["a"],
        " age": [1],
        " city ": ["x"]
    })
    ).to_excel(excel_path, index=False)

    payload = extract_columns(excel_path)

    result = json.loads(payload)
    assert result["sheet 1 (Sheet1)"]["column_names"] == ["name", "age", "city"]
    assert result["sheet 1 (Sheet1)"]["column_positions"] == [["A", 1], ["B", 1], ["C", 1]]
    assert result["sheet 1 (Sheet1)"]["column_count"] == 3
    assert result["sheet 1 (Sheet1)"]["columns"][0]["cell_ref"] == "A1"

# Tests that extract_columns handles nested data structure in cells by treating them as string representations without causing errors.
# Calls extract_columns.
def test_extract_columns_nested_data(tmp_path: Path):
    excel_path = tmp_path / "nested_data.xlsx"
    pd.DataFrame({
        "name": ["a"],
        "age": [1],
        "city": ["x"],
        "details": [{"hobby": "reading", "pet": "cat"}]
    }).to_excel(excel_path, index=False)

    payload = extract_columns(excel_path)

    result = json.loads(payload)
    assert result["sheet 1 (Sheet1)"]["column_names"] == ["name", "age", "city", "details"]
    assert result["sheet 1 (Sheet1)"]["column_positions"] == [["A", 1], ["B", 1], ["C", 1], ["D", 1]]
    assert result["sheet 1 (Sheet1)"]["column_count"] == 4
    assert result["sheet 1 (Sheet1)"]["columns"][3]["name"] == "details"

# Tests that extract_columns will skip the first non-empty cell if it contains an Excel error (#N/A) and search for the next non-empty cell in the column.
# Calls extract_columns.
def test_extract_columns_with_excel_errors(tmp_path: Path):
    excel_path = tmp_path / "excel_errors.xlsx"
    pd.DataFrame([
        [np.nan, "a"], # Excel error #N/A is represented as empty cell (NaN).
        [None,1]
    ]).to_excel (excel_path, index=False, header=False)

    payload = extract_columns(excel_path)
    result = json.loads(payload)

    sheet = result["sheet 1 (Sheet1)"]
    assert sheet["column_names"] == ["a"]
    assert sheet["column_positions"] == [["B", 1]]
    assert sheet["column_count"] == 1
    assert sheet["columns"][0]["cell_ref"] == "B1"
