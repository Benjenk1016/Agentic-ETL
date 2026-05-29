"""
Utilities for analyzing Excel/CSV files to extract metadata and detect inconsistencies.
Used by the analyze endpoint to provide rich context to the LLM.
"""

import json
from statistics import mean
from typing import Dict, List, Any

import pandas as pd


def infer_data_type(series: pd.Series) -> str:
    """
    Infer the likely data type of a pandas Series.
    Returns: 'int', 'float', 'date', 'text', 'boolean', or 'mixed'
    """
    if series.empty:
        return 'unknown'
    
    # Check for null/NaN
    non_null = series.dropna()
    if non_null.empty:
        return 'empty'
    
    # Check for boolean
    if series.dtype == bool or set(non_null.unique()).issubset({True, False, 'True', 'False', 'YES', 'NO', 'yes', 'no'}):
        return 'boolean'
    
    # Check for integer
    if pd.api.types.is_integer_dtype(series):
        return 'int'
    
    # Check for float
    if pd.api.types.is_float_dtype(series):
        return 'float'
    
    # Check for datetime
    try:
        pd.to_datetime(non_null)
        return 'date'
    except:
        pass
    
    # Check if all values are numeric (as strings)
    try:
        pd.to_numeric(non_null)
        return 'numeric_text'
    except:
        pass
    
    # Default to text
    return 'text'


def extract_column_metadata(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """
    Extract detailed metadata for each column in a DataFrame.
    
    Returns a dict where each key is a column name, and value contains:
    - data_type: Inferred data type
    - row_count: Total rows in column
    - null_count: Number of null/NaN values
    - unique_count: Number of unique values
    - sample_values: Up to 5 sample non-null values
    """
    metadata = {}
    
    for col in df.columns:
        series = df[col]
        non_null = series.dropna()
        
        # Get sample values
        samples = non_null.unique()[:5].tolist() if len(non_null) > 0 else []
        
        metadata[col] = {
            'data_type': infer_data_type(series),
            'row_count': len(series),
            'null_count': series.isna().sum(),
            'unique_count': series.nunique(),
            'sample_values': samples,
        }
    
    return metadata


def detect_column_inconsistencies(input_metadata: Dict, config_metadata: Dict) -> Dict[str, List[str]]:
    """
    Compare input and config metadata to detect inconsistencies.
    
    Returns:
    {
        'missing_in_input': ['Column names that are in config but not in input'],
        'extra_in_input': ['Column names in input but not in config'],
        'type_mismatch': ['Column: expected X, got Y'],
        'potential_renames': [('Input col', 'Config col', 'similarity_score'), ...],
    }
    """
    input_cols = set(input_metadata.keys())
    config_cols = set(config_metadata.keys())
    
    inconsistencies = {
        'missing_in_input': list(config_cols - input_cols),
        'extra_in_input': list(input_cols - config_cols),
        'type_mismatch': [],
        'potential_renames': [],
    }
    
    # Check for type mismatches in matching columns
    for col in input_cols & config_cols:
        input_type = input_metadata[col]['data_type']
        config_type = config_metadata[col]['data_type']
        
        # Allow some flexibility: numeric_text vs int/float is OK
        compatible_types = [
            {'int', 'numeric_text'},
            {'float', 'numeric_text'},
            {'int', 'float'},
        ]
        
        type_pair = {input_type, config_type}
        if input_type != config_type and type_pair not in compatible_types:
            inconsistencies['type_mismatch'].append(
                f"{col}: expected {config_type}, got {input_type}"
            )
    
    # Detect potential renames (columns with similar names)
    if inconsistencies['missing_in_input'] and inconsistencies['extra_in_input']:
        for missing_col in inconsistencies['missing_in_input']:
            for extra_col in inconsistencies['extra_in_input']:
                # Simple similarity: check for substring or common prefix
                missing_lower = missing_col.lower().replace('_', ' ')
                extra_lower = extra_col.lower().replace('_', ' ')
                
                # Calculate overlap
                if (missing_lower in extra_lower or extra_lower in missing_lower or
                    missing_lower[:3] == extra_lower[:3]):  # First 3 chars match
                    inconsistencies['potential_renames'].append(
                        (extra_col, missing_col)
                    )
    
    return inconsistencies


def format_metadata_for_llm(df_metadata: Dict[str, Dict], file_name: str, file_type: str = 'Input') -> str:
    """
    Format extracted metadata into a readable string for the LLM prompt.
    """
    lines = [f"\n{file_type} FILE: {file_name}"]
    lines.append(f"Columns ({len(df_metadata)}): {', '.join(df_metadata.keys())}")
    lines.append("")
    
    for col_name, col_data in df_metadata.items():
        lines.append(f"  • {col_name}")
        lines.append(f"    - Type: {col_data['data_type']}")
        lines.append(f"    - Rows: {col_data['row_count']}, Nulls: {col_data['null_count']}, Unique: {col_data['unique_count']}")
        
        samples_str = ", ".join(str(s)[:20] for s in col_data['sample_values'])
        lines.append(f"    - Samples: {samples_str if samples_str else 'N/A'}")
    
    return "\n".join(lines)


def format_inconsistencies_for_llm(inconsistencies: Dict[str, Any]) -> str:
    """
    Format detected inconsistencies into a readable string for the LLM prompt.
    """
    lines = ["\nDETECTED INCONSISTENCIES (Potential Issues):"]
    
    if not any(inconsistencies.values()):
        lines.append("  ✓ No inconsistencies detected automatically")
        return "\n".join(lines)
    
    if inconsistencies['missing_in_input']:
        lines.append("\n  1. MISSING IN INPUT (Config expects these, but Input doesn't have them):")
        for col in inconsistencies['missing_in_input']:
            lines.append(f"     - {col}")
    
    if inconsistencies['extra_in_input']:
        lines.append("\n  2. EXTRA IN INPUT (Input has these, but Config doesn't expect them):")
        for col in inconsistencies['extra_in_input']:
            lines.append(f"     - {col}")
    
    if inconsistencies['type_mismatch']:
        lines.append("\n  3. DATA TYPE MISMATCHES:")
        for mismatch in inconsistencies['type_mismatch']:
            lines.append(f"     - {mismatch}")
    
    if inconsistencies['potential_renames']:
        lines.append("\n  4. POTENTIAL COLUMN RENAMES (possible typos or naming variations):")
        for input_col, config_col in inconsistencies['potential_renames']:
            lines.append(f"     - '{input_col}' might be '{config_col}'")
    
    return "\n".join(lines)


def compare_payload_columns(input_payload: Dict[str, Any], config_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compare normalized column-name sets between two extract_columns payloads."""
    def _column_set(payload: Dict[str, Any]) -> set[str]:
        names: set[str] = set()
        if not isinstance(payload, dict):
            return names
        for sheet_data in payload.values():
            if not isinstance(sheet_data, dict):
                continue
            for value in sheet_data.get("column_names", []):
                normalized = str(value).strip().lower()
                if normalized:
                    names.add(normalized)
        return names

    input_columns = _column_set(input_payload)
    config_columns = _column_set(config_payload)

    shared = sorted(input_columns & config_columns)
    missing_in_input = sorted(config_columns - input_columns)
    extra_in_input = sorted(input_columns - config_columns)
    overlap_ratio = round((len(shared) / len(config_columns)), 3) if config_columns else 1.0

    return {
        "shared_columns": shared,
        "missing_in_input": missing_in_input,
        "extra_in_input": extra_in_input,
        "overlap_ratio_vs_config": overlap_ratio,
    }


def format_payload_for_llm(payload: Dict[str, Any], file_name: str, file_type: str) -> str:
    """Render extract_columns payload in a compact, prompt-friendly format."""
    lines: list[str] = [f"\n{file_type} FILE: {file_name}", "EXTRACT_COLUMNS PAYLOAD:"]

    if not isinstance(payload, dict) or not payload:
        lines.append("(no sheet data)")
        return "\n".join(lines)

    for sheet_name, sheet_data in payload.items():
        if not isinstance(sheet_data, dict):
            continue

        column_names = sheet_data.get("column_names", [])
        column_positions = sheet_data.get("column_positions", [])
        column_details = sheet_data.get("columns", [])

        if not isinstance(column_names, list):
            column_names = []
        if not isinstance(column_positions, list):
            column_positions = []
        if not isinstance(column_details, list):
            column_details = []

        positions_inline: list[str] = []
        if column_details and all(isinstance(col, dict) and isinstance(col.get("cell_ref"), str) for col in column_details):
            formatted_names: list[str] = []
            for col in column_details:
                if not isinstance(col, dict):
                    continue

                name = str(col.get("name", "")).strip()
                annotations: list[str] = []
                if col.get("marked_with_use_this") is True:
                    marker_row = col.get("use_this_marker_row")
                    if isinstance(marker_row, int):
                        annotations.append(f"USE THIS marker row {marker_row}")
                    else:
                        annotations.append("USE THIS marker")
                if col.get("is_duplicate") is True:
                    dup_pos = col.get("duplicate_position")
                    if isinstance(dup_pos, int):
                        annotations.append(f"duplicate #{dup_pos}")

                if annotations:
                    formatted_names.append(f"{name} [{' | '.join(annotations)}]")
                else:
                    formatted_names.append(name)

                if col.get("cell_ref"):
                    positions_inline.append(f"{col['column_number']} ({col['cell_ref']})")

            names_inline = ", ".join(formatted_names) if formatted_names else "(none)"
        else:
            for pos in column_positions:
                if isinstance(pos, list) and len(pos) == 2:
                    positions_inline.append(f"{pos[0]}{pos[1]}")
            names_inline = ", ".join(str(name) for name in column_names) if column_names else "(none)"

        pos_inline = ", ".join(positions_inline) if positions_inline else "(none)"

        lines.append(f"- {sheet_name}")
        lines.append(f"  columns: {names_inline}")
        lines.append(f"  positions: {pos_inline}")

        header_row = sheet_data.get("header_row")
        data_start_row = sheet_data.get("data_start_row")
        if isinstance(header_row, int):
            lines.append(f"  header_row: {header_row}")
        if isinstance(data_start_row, int):
            lines.append(f"  data_start_row: {data_start_row}")

    return "\n".join(lines)
