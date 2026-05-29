#!/usr/bin/env python
"""
Integration test for the analyze endpoint.
Uses real-ish files from data/input and data/config to validate detection.
Run with: python -m backend.tests.test_analyze_integration
"""

import pandas as pd
from pathlib import Path
import sys

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.api.analysis_utils import (
    extract_column_metadata,
    detect_column_inconsistencies,
    format_metadata_for_llm,
    format_inconsistencies_for_llm,
)


def get_repo_paths():
    """Resolve key paths used by this integration test."""
    repo_root = Path(__file__).parent.parent.parent
    return {
        "repo_root": repo_root,
        "input_dir": repo_root / "data" / "input",
        "config_dir": repo_root / "data" / "config",
    }


def discover_real_file_pairs():
    """Discover real-ish input/config files from repository data folders."""
    paths = get_repo_paths()
    input_dir = paths["input_dir"]
    config_dir = paths["config_dir"]

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    if not config_dir.exists():
        raise FileNotFoundError(f"Config directory not found: {config_dir}")

    input_files = sorted(
        [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in {".xlsx", ".xls", ".csv"}]
    )
    config_files = sorted(
        [p for p in config_dir.iterdir() if p.is_file() and p.suffix.lower() in {".xlsx", ".xls", ".csv"}]
    )

    if not input_files:
        raise ValueError(f"No input files found in {input_dir}")
    if not config_files:
        raise ValueError(f"No config files found in {config_dir}")

    # Current repository has one canonical config workbook; apply it to each input file.
    primary_config = config_files[0]
    scenarios = []
    for idx, input_file in enumerate(input_files, start=1):
        desc = f"Real Data Scenario {idx}: {input_file.name} vs {primary_config.name}"
        scenarios.append((input_file, primary_config, desc))

    return scenarios


def read_tabular_file(file_path):
    """Read CSV/Excel file; for Excel concatenate non-empty sheets."""
    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(file_path)

    if suffix in {".xlsx", ".xls"}:
        excel_data = pd.read_excel(file_path, sheet_name=None)
        non_empty = [df for df in excel_data.values() if not df.empty]
        if not non_empty:
            raise ValueError(f"All sheets are empty in {file_path.name}")
        return pd.concat(non_empty, ignore_index=True)

    raise ValueError(f"Unsupported file type for {file_path.name}")


def analyze_scenario(input_path, config_path, description):
    """Analyze a test scenario and print results"""
    print(f"\n{'='*70}")
    print(f"[>] {description}")
    print(f"{'='*70}")
    
    # Read files
    input_df = read_tabular_file(input_path)
    config_df = read_tabular_file(config_path)
    
    print(f"\n[INFO] Input File:")
    print(f"   Columns: {list(input_df.columns)}")
    print(f"   Rows: {len(input_df)}")
    
    print(f"\n[INFO] Config File:")
    print(f"   Columns: {list(config_df.columns)}")
    print(f"   Rows: {len(config_df)}")
    
    # Extract metadata
    input_meta = extract_column_metadata(input_df)
    config_meta = extract_column_metadata(config_df)
    
    # Detect inconsistencies
    incon = detect_column_inconsistencies(input_meta, config_meta)
    
    # Format output
    incon_str = format_inconsistencies_for_llm(incon)
    
    print(f"\n{incon_str}")
    
    # Format for LLM
    input_meta_str = format_metadata_for_llm(input_meta, input_path.name, "INPUT")
    config_meta_str = format_metadata_for_llm(config_meta, config_path.name, "CONFIG")
    
    print(f"\n{input_meta_str}")
    print(f"\n{config_meta_str}")
    
    return incon


def run_all_scenarios():
    """Run all test scenarios and validate detection"""
    print("\n[*] ETL Analysis Integration Tests")
    print("="*70)
    
    # Discover real data scenarios from repository folders
    scenarios = discover_real_file_pairs()
    print(f"[INFO] Found {len(scenarios)} input file(s) in data/input")
    print(f"[INFO] Using config file: {scenarios[0][1].name}")
    
    results = []
    
    # Run each scenario
    for input_file, config_file, description in scenarios:
        incon = analyze_scenario(input_file, config_file, description)
        results.append((description, incon))
    
    # Validate results
    print(f"\n{'='*70}")
    print("[+] VALIDATION RESULTS")
    print(f"{'='*70}")

    issue_count = 0
    for description, incon in results:
        required_keys = {"missing_in_input", "extra_in_input", "type_mismatch", "potential_renames"}
        assert required_keys.issubset(incon.keys()), f"{description}: Missing expected inconsistency keys"
        has_issues = any(
            len(incon[key]) > 0 for key in required_keys
        )
        if has_issues:
            issue_count += 1
        print(f"[PASS] {description}: Analysis completed")

    print(f"[INFO] Scenarios with detected issues: {issue_count}/{len(results)}")
    assert issue_count > 0, "Expected at least one scenario to contain detectable inconsistencies"
    
    print(f"\n{'='*70}")
    print("[SUCCESS] All tests passed! Real-data analysis utilities working correctly.")
    print(f"{'='*70}")


if __name__ == '__main__':
    try:
        run_all_scenarios()
    except AssertionError as e:
        print(f"\n[FAIL] Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
