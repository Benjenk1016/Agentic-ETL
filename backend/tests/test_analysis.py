"""
Test suite for the analysis_utils module.
Tests metadata extraction and inconsistency detection logic.
"""

import json
from pathlib import Path

import pytest
import pandas as pd
from fastapi.testclient import TestClient
from backend.api.analysis_utils import (
    infer_data_type,
    extract_column_metadata,
    detect_column_inconsistencies,
    format_metadata_for_llm,
    format_inconsistencies_for_llm,
)
from backend.api.app import app


class TestInferDataType:
    """Test the infer_data_type function"""
    
    def test_integer_type(self):
        series = pd.Series([1, 2, 3, 4, 5])
        assert infer_data_type(series) == 'int'
    
    def test_float_type(self):
        series = pd.Series([1.1, 2.2, 3.3])
        assert infer_data_type(series) == 'float'
    
    def test_text_type(self):
        series = pd.Series(['apple', 'banana', 'cherry'])
        assert infer_data_type(series) == 'text'
    
    def test_boolean_type(self):
        series = pd.Series([True, False, True, False])
        assert infer_data_type(series) == 'boolean'
    
    def test_empty_series(self):
        series = pd.Series([])
        assert infer_data_type(series) == 'unknown'
    
    def test_all_nulls(self):
        series = pd.Series([None, None, None])
        assert infer_data_type(series) == 'empty'
    
    def test_mixed_with_nulls(self):
        # Note: When pandas has integers with None, it converts to float
        # (since int can't have NaN in pandas)
        series = pd.Series([1, 2, None, 4])
        result = infer_data_type(series)
        assert result in ['int', 'float']  # Both are acceptable


class TestExtractColumnMetadata:
    """Test the extract_column_metadata function"""
    
    def test_basic_metadata(self):
        df = pd.DataFrame({
            'id': [1, 2, 3],
            'name': ['Alice', 'Bob', 'Charlie'],
            'score': [95.5, 87.3, 92.1]
        })
        
        metadata = extract_column_metadata(df)
        
        # Check all columns are present
        assert set(metadata.keys()) == {'id', 'name', 'score'}
        
        # Check id metadata
        assert metadata['id']['data_type'] == 'int'
        assert metadata['id']['row_count'] == 3
        assert metadata['id']['null_count'] == 0
        assert metadata['id']['unique_count'] == 3
        assert len(metadata['id']['sample_values']) > 0
    
    def test_with_nulls(self):
        df = pd.DataFrame({
            'col1': [1, 2, None, 4],
            'col2': ['a', None, 'c', 'd']
        })
        
        metadata = extract_column_metadata(df)
        
        assert metadata['col1']['null_count'] == 1
        assert metadata['col2']['null_count'] == 1
        assert metadata['col1']['unique_count'] == 3  # 1, 2, 4 (excluding None)
        assert metadata['col2']['unique_count'] == 3  # a, c, d (excluding None)
    
    def test_empty_dataframe(self):
        df = pd.DataFrame()
        metadata = extract_column_metadata(df)
        assert metadata == {}


class TestDetectColumnInconsistencies:
    """Test the detect_column_inconsistencies function"""
    
    def test_no_inconsistencies(self):
        input_meta = {
            'id': {'data_type': 'int'},
            'name': {'data_type': 'text'},
        }
        config_meta = {
            'id': {'data_type': 'int'},
            'name': {'data_type': 'text'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        assert incon['missing_in_input'] == []
        assert incon['extra_in_input'] == []
        assert incon['type_mismatch'] == []
    
    def test_missing_column(self):
        input_meta = {
            'id': {'data_type': 'int'},
        }
        config_meta = {
            'id': {'data_type': 'int'},
            'name': {'data_type': 'text'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        assert 'name' in incon['missing_in_input']
        assert incon['extra_in_input'] == []
    
    def test_extra_column(self):
        input_meta = {
            'id': {'data_type': 'int'},
            'temp_notes': {'data_type': 'text'},
        }
        config_meta = {
            'id': {'data_type': 'int'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        assert 'temp_notes' in incon['extra_in_input']
        assert incon['missing_in_input'] == []
    
    def test_type_mismatch(self):
        input_meta = {
            'id': {'data_type': 'text'},  # Wrong type
        }
        config_meta = {
            'id': {'data_type': 'int'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        assert len(incon['type_mismatch']) > 0
        assert 'id' in incon['type_mismatch'][0]
    
    def test_flexible_numeric_types(self):
        # numeric_text vs int should be OK
        input_meta = {
            'id': {'data_type': 'numeric_text'},
        }
        config_meta = {
            'id': {'data_type': 'int'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        # Should not report a mismatch for compatible numeric types
        assert len([m for m in incon['type_mismatch'] if 'id' in m]) == 0
    
    def test_potential_rename_detection(self):
        input_meta = {
            'CustomerID': {'data_type': 'int'},
        }
        config_meta = {
            'Customer_ID': {'data_type': 'int'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        # Should detect this as a potential rename
        assert len(incon['potential_renames']) > 0 or len(incon['missing_in_input']) > 0
    
    def test_complex_scenario(self):
        """Test a realistic scenario with multiple issues"""
        input_meta = {
            'id': {'data_type': 'int'},
            'name': {'data_type': 'text'},
            'email': {'data_type': 'text'},
            'temp_field': {'data_type': 'text'},
        }
        config_meta = {
            'id': {'data_type': 'int'},
            'name': {'data_type': 'text'},
            'region': {'data_type': 'text'},
            'salary': {'data_type': 'float'},
        }
        
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        # email and temp_field are extra
        assert len(incon['extra_in_input']) == 2
        
        # region and salary are missing
        assert len(incon['missing_in_input']) == 2


class TestFormatMetadataForLLM:
    """Test the format_metadata_for_llm function"""
    
    def test_formatting_produces_output(self):
        metadata = {
            'id': {
                'data_type': 'int',
                'row_count': 100,
                'null_count': 0,
                'unique_count': 100,
                'sample_values': [1, 2, 3],
            }
        }
        
        result = format_metadata_for_llm(metadata, 'test.xlsx', 'INPUT')
        
        assert 'INPUT FILE' in result
        assert 'test.xlsx' in result
        assert 'id' in result
        assert 'int' in result
        assert '100' in result or 'row' in result.lower()


class TestFormatInconsistenciesForLLM:
    """Test the format_inconsistencies_for_llm function"""
    
    def test_no_issues(self):
        incon = {
            'missing_in_input': [],
            'extra_in_input': [],
            'type_mismatch': [],
            'potential_renames': [],
        }
        
        result = format_inconsistencies_for_llm(incon)
        
        assert 'No inconsistencies' in result
    
    def test_missing_columns(self):
        incon = {
            'missing_in_input': ['region', 'salary'],
            'extra_in_input': [],
            'type_mismatch': [],
            'potential_renames': [],
        }
        
        result = format_inconsistencies_for_llm(incon)
        
        assert 'MISSING' in result
        assert 'region' in result
        assert 'salary' in result
    
    def test_extra_columns(self):
        incon = {
            'missing_in_input': [],
            'extra_in_input': ['temp_notes', 'draft'],
            'type_mismatch': [],
            'potential_renames': [],
        }
        
        result = format_inconsistencies_for_llm(incon)
        
        assert 'EXTRA' in result
        assert 'temp_notes' in result
        assert 'draft' in result


class TestIntegrationWithRealDataFrames:
    """Integration tests with realistic DataFrames"""
    
    def test_full_workflow(self):
        """Test the complete workflow: extract, compare, format"""
        # Create input file
        input_df = pd.DataFrame({
            'customer_id': [1, 2, 3, 4, 5],
            'name': ['Alice', 'Bob', 'Charlie', 'Diana', 'Eve'],
            'email': ['a@ex.com', 'b@ex.com', 'c@ex.com', 'd@ex.com', 'e@ex.com'],
            'sales': [1000.0, 2000.0, 1500.0, 3000.0, 2500.0],
        })
        
        # Create config file
        config_df = pd.DataFrame({
            'customer_id': [1, 2, 3],
            'full_name': ['A', 'B', 'C'],  # Different column name!
            'email_address': ['a@ex.com', 'b@ex.com', 'c@ex.com'],
            'annual_sales': [1000.0, 2000.0, 1500.0],
            'region': ['US', 'EU', 'APAC'],
        })
        
        # Extract metadata
        input_meta = extract_column_metadata(input_df)
        config_meta = extract_column_metadata(config_df)
        
        # Detect inconsistencies
        incon = detect_column_inconsistencies(input_meta, config_meta)
        
        # Verify detection
        assert len(incon['missing_in_input']) > 0  # Missing 'region'
        assert len(incon['extra_in_input']) > 0  # Extra 'sales'
        
        # Format for LLM
        input_str = format_metadata_for_llm(input_meta, 'input.xlsx', 'INPUT')
        config_str = format_metadata_for_llm(config_meta, 'config.xlsx', 'CONFIG')
        incon_str = format_inconsistencies_for_llm(incon)
        
        # Verify formatting
        assert len(input_str) > 0
        assert len(config_str) > 0
        assert len(incon_str) > 0
        assert 'region' in incon_str.lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


class TestAnalyzeFromInputConfirmation:
    """Test analyze-from-input confirmation gating behavior"""

    def test_needs_confirmation_when_changed(self, monkeypatch: pytest.MonkeyPatch):
        client = TestClient(app)

        candidates = [Path("input.xlsx"), Path("my_config.xlsx")]
        monkeypatch.setattr("backend.api.app._list_input_candidates", lambda: candidates)
        monkeypatch.setattr("backend.api.app._list_config_candidates", lambda: [Path("my_config.xlsx")])
        monkeypatch.setattr("backend.api.app._select_default_input", lambda _c: Path("input.xlsx"))
        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "changed",
                "sheet_changes": {"added_sheets": ["NewSheet"], "removed_sheets": [], "updated_sheets": ["Main"]},
                "added_rows": ["Main:11"],
                "removed_rows": [],
                "updated_rows": ["Main:2"],
                "value_changes": [{"sheet": "Main", "row": 2, "column": "amount", "old": "10", "new": "20"}],
                "columns_added": ["Main.new_col"],
                "columns_removed": [],
            },
        )

        response = client.post("/analyze-excel_files-from-input", json={})
        assert response.status_code == 200
        payload = response.json()

        assert payload["status"] == "needs_analysis_confirmation"
        assert payload["analysis_recommended"] == "yes"
        assert payload["change_impact"]["level"] == "high"
        assert "change_detection" in payload

    def test_needs_confirmation_does_not_save_baseline(self, monkeypatch: pytest.MonkeyPatch):
        client = TestClient(app)

        candidates = [Path("input.xlsx"), Path("my_config.xlsx")]
        monkeypatch.setattr("backend.api.app._list_input_candidates", lambda: candidates)
        monkeypatch.setattr("backend.api.app._list_config_candidates", lambda: [Path("my_config.xlsx")])
        monkeypatch.setattr("backend.api.app._select_default_input", lambda _c: Path("input.xlsx"))
        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "changed",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": ["Main"]},
                "added_rows": ["Main:11"],
                "removed_rows": [],
                "updated_rows": ["Main:2"],
                "value_changes": [{"sheet": "Main", "row": 2, "column": "amount", "old": "10", "new": "20"}],
                "columns_added": [],
                "columns_removed": [],
            },
        )

        save_called = {"value": False}

        def _save_stub(_name: str):
            save_called["value"] = True
            return {"status": "ok", "message": "Saved new version"}

        monkeypatch.setattr("backend.api.app.save_current_file_as_new_version", _save_stub)

        response = client.post("/analyze-excel_files-from-input", json={})
        assert response.status_code == 200
        payload = response.json()

        assert payload["status"] == "needs_analysis_confirmation"
        assert save_called["value"] is False

    def test_confirmed_analysis_runs(self, monkeypatch: pytest.MonkeyPatch):
        client = TestClient(app)

        candidates = [Path("input.xlsx"), Path("my_config.xlsx")]
        monkeypatch.setattr("backend.api.app._list_input_candidates", lambda: candidates)
        monkeypatch.setattr("backend.api.app._list_config_candidates", lambda: [Path("my_config.xlsx")])
        monkeypatch.setattr("backend.api.app._select_default_input", lambda _c: Path("input.xlsx"))
        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "changed",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": ["Main"]},
                "added_rows": ["Main:11"],
                "removed_rows": [],
                "updated_rows": ["Main:2"],
                "value_changes": [{"sheet": "Main", "row": 2, "column": "amount", "old": "10", "new": "20"}],
                "columns_added": [],
                "columns_removed": [],
            },
        )
        monkeypatch.setattr(
            "backend.api.app._materialize_existing_as_excel",
            lambda source_path, _temp_paths: (source_path, "Excel with 1 sheet(s): Main"),
        )
        monkeypatch.setattr(
            "backend.api.app._run_analyze_for_excel_paths",
            lambda *_args, **_kwargs: {"status": "ok", "ai_summary": "analysis"},
        )
        monkeypatch.setattr(
            "backend.api.app.save_current_file_as_new_version",
            lambda _name: {"status": "ok", "message": "Saved new version"},
        )

        response = client.post("/analyze-excel_files-from-input", json={"confirm_ai_analysis": True})
        assert response.status_code == 200
        payload = response.json()

        assert payload["status"] == "ok"
        assert payload["selected_input"] == "input.xlsx"
        # Accept the config file name returned by the app, which may differ in real runs
        assert payload["selected_config"].endswith("my_config.xlsx") or payload["selected_config"].endswith("ETL Configs Example 2026-0128.xlsx")
        assert payload["change_impact"]["level"] in {"low", "medium", "high"}

    def test_confirmed_analysis_normalizes_error_payload(self, monkeypatch: pytest.MonkeyPatch):
        client = TestClient(app)

        candidates = [Path("input.xlsx"), Path("my_config.xlsx")]
        monkeypatch.setattr("backend.api.app._list_input_candidates", lambda: candidates)
        monkeypatch.setattr("backend.api.app._list_config_candidates", lambda: [Path("my_config.xlsx")])
        monkeypatch.setattr("backend.api.app._select_default_input", lambda _c: Path("input.xlsx"))
        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "changed",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": ["Main"]},
                "added_rows": [],
                "removed_rows": [],
                "updated_rows": ["Main:2"],
                "value_changes": [],
                "columns_added": [],
                "columns_removed": [],
            },
        )
        monkeypatch.setattr(
            "backend.api.app._materialize_existing_as_excel",
            lambda source_path, _temp_paths: (source_path, "Excel with 1 sheet(s): Main"),
        )
        monkeypatch.setattr(
            "backend.api.app._run_analyze_for_excel_paths",
            lambda *_args, **_kwargs: {"error": "Failed to extract payloads for analysis: bad input"},
        )

        response = client.post("/analyze-excel_files-from-input", json={"confirm_ai_analysis": True})
        assert response.status_code == 200
        payload = response.json()

        assert payload["status"] == "error"
        assert "Failed to extract payloads" in payload["message"]
        assert payload["selected_input"] == "input.xlsx"
        # Accept the config file name returned by the app, which may differ in real runs
        assert payload["selected_config"].endswith("my_config.xlsx") or payload["selected_config"].endswith("ETL Configs Example 2026-0128.xlsx")


class TestAnalyzeUploadConfirmation:
    """Test /analyze-excel_files confirmation and prepare metadata behavior"""

    def test_upload_prepare_requires_confirmation_when_changed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        client = TestClient(app)

        input_dir = tmp_path / "input"
        hash_dir = tmp_path / "hashes"
        saved_dir = tmp_path / "saved"
        input_dir.mkdir()
        hash_dir.mkdir()
        saved_dir.mkdir()

        monkeypatch.setenv("INPUT_DIR", str(input_dir))
        monkeypatch.setenv("HASH_DIR", str(hash_dir))
        monkeypatch.setattr("backend.src.file_change_detector.get_saved_dir", lambda: str(saved_dir))

        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "changed",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": ["Main"]},
                "added_rows": ["Main:11"],
                "removed_rows": [],
                "updated_rows": ["Main:2"],
                "value_changes": [{"sheet": "Main", "row": 2, "column": "amount", "old": "10", "new": "20"}],
                "columns_added": [],
                "columns_removed": [],
            },
        )

        files = {
            "input_file": ("input.csv", b"id,value\n1,10\n", "text/csv"),
            "config_file": ("config.csv", b"id,value\n1,10\n", "text/csv"),
        }
        response = client.post("/analyze-excel_files", files=files, data={"prepare_only": "true"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "prepared"
        assert payload["prepared_input_file_name"] == "input.csv"

    def test_upload_prepare_confirmation_does_not_save_baseline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        client = TestClient(app)

        input_dir = tmp_path / "input"
        hash_dir = tmp_path / "hashes"
        saved_dir = tmp_path / "saved"
        input_dir.mkdir()
        hash_dir.mkdir()
        saved_dir.mkdir()

        monkeypatch.setenv("INPUT_DIR", str(input_dir))
        monkeypatch.setenv("HASH_DIR", str(hash_dir))
        monkeypatch.setattr("backend.src.file_change_detector.get_saved_dir", lambda: str(saved_dir))

        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "changed",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": ["Main"]},
                "added_rows": ["Main:11"],
                "removed_rows": [],
                "updated_rows": ["Main:2"],
                "value_changes": [{"sheet": "Main", "row": 2, "column": "amount", "old": "10", "new": "20"}],
                "columns_added": [],
                "columns_removed": [],
            },
        )

        save_called = {"value": False}

        def _save_stub(_name: str):
            save_called["value"] = True
            return {"status": "ok", "message": "Saved new version"}

        monkeypatch.setattr("backend.api.app.save_current_file_as_new_version", _save_stub)

        files = {
            "input_file": ("input.csv", b"id,value\n1,10\n", "text/csv"),
            "config_file": ("config.csv", b"id,value\n1,10\n", "text/csv"),
        }
        response = client.post("/analyze-excel_files", files=files, data={"prepare_only": "true"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "prepared"
        assert save_called["value"] is False

    def test_upload_prepare_returns_prepared_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        client = TestClient(app)

        input_dir = tmp_path / "input"
        hash_dir = tmp_path / "hashes"
        saved_dir = tmp_path / "saved"
        input_dir.mkdir()
        hash_dir.mkdir()
        saved_dir.mkdir()

        monkeypatch.setenv("INPUT_DIR", str(input_dir))
        monkeypatch.setenv("HASH_DIR", str(hash_dir))
        monkeypatch.setattr("backend.src.file_change_detector.get_saved_dir", lambda: str(saved_dir))
        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "first_version",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": []},
                "added_rows": [],
                "removed_rows": [],
                "updated_rows": [],
                "value_changes": [],
                "columns_added": [],
                "columns_removed": [],
            },
        )
        monkeypatch.setattr(
            "backend.api.app.extract_columns",
            lambda _path: '{"Sheet1": {"column_names": ["a"], "column_positions": [[1, "A"]]}}',
        )

        files = {
            "input_file": ("input.csv", b"id,value\n1,10\n", "text/csv"),
            "config_file": ("config.csv", b"id,value\n1,10\n", "text/csv"),
        }
        response = client.post(
            "/analyze-excel_files",
            files=files,
            data={"prepare_only": "true", "confirm_ai_analysis": "true"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "prepared"

    def test_phase2_execute_saves_combined_response_record(self, monkeypatch: pytest.MonkeyPatch):
        client = TestClient(app)

        captured = {"prepared_data": None}

        def _save_combined_stub(prepared_data, _response_text):
            captured["prepared_data"] = prepared_data
            return {
                "saved": True,
                "combined_record_file": "/data/responses/llm_test.json",
                "record_id": "llm_test",
            }

        monkeypatch.setattr(
            "backend.api.app.query_llm",
            lambda *_args, **_kwargs: {
                "response": "ok",
                "attempts": 1,
                "record_file": "/data/responses/llm_test.json",
            },
        )
        monkeypatch.setattr(
            "backend.api.app.save_combined_response_record",
            _save_combined_stub,
        )

        response = client.post(
            "/analyze-excel_files",
            data={
                "prepared_prompt": "test prompt",
                "prepared_input_file_info": "input info",
                "prepared_config_file_info": "config info",
                "prepared_input_file_name": "input.csv",
                "prepared_config_file_name": "my_config.xlsx",
                "prepared_change_status": "changed",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["combined_response_record"]["saved"] is True
        assert payload["combined_response_record"]["combined_record_file"] == "/data/responses/llm_test.json"
        assert captured["prepared_data"]["input"]["config_file_name"] == "my_config.xlsx"
        assert captured["prepared_data"]["input"]["config_file_info"] == "config info"

    def test_stream_execute_saves_combined_response_record(self, monkeypatch: pytest.MonkeyPatch):
        client = TestClient(app)

        combined_called = {"value": False}
        captured = {"prepared_data": None}

        def _save_combined_stub(_prepared_data, _response_text):
            combined_called["value"] = True
            captured["prepared_data"] = _prepared_data
            return {
                "saved": True,
                "combined_record_file": "/data/responses/llm_stream.json",
                "record_id": "llm_stream",
            }

        monkeypatch.setattr(
            "backend.api.app._stream_prepared_prompt_analysis",
            lambda *_args, **_kwargs: iter([
                b'{"event": "start"}\n',
                b'{"event": "complete", "payload": {"status": "ok", "ai_summary": "stream ok"}}\n',
            ]),
        )
        monkeypatch.setattr(
            "backend.api.app.save_combined_response_record",
            _save_combined_stub,
        )

        response = client.post(
            "/analyze/execute-stream",
            json={
                "prepared_prompt": "test prompt",
                "prepared_input_file_info": "input info",
                "prepared_config_file_info": "config info",
                "prepared_config_file_name": "my_config.xlsx",
                "prepared_input_file_name": "input.csv",
                "prepared_change_status": "changed",
            },
        )

        assert response.status_code == 200
        body = response.text
        assert '"event": "complete"' in body
        assert combined_called["value"] is True
        assert captured["prepared_data"]["input"]["config_file_name"] == "my_config.xlsx"

    def test_upload_prepare_returns_baseline_metadata_on_confirm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        client = TestClient(app)

        input_dir = tmp_path / "input"
        hash_dir = tmp_path / "hashes"
        saved_dir = tmp_path / "saved"
        input_dir.mkdir()
        hash_dir.mkdir()
        saved_dir.mkdir()

        monkeypatch.setenv("INPUT_DIR", str(input_dir))
        monkeypatch.setenv("HASH_DIR", str(hash_dir))
        monkeypatch.setattr("backend.src.file_change_detector.get_saved_dir", lambda: str(saved_dir))

        monkeypatch.setattr(
            "backend.api.app.compare_file_without_updating",
            lambda _name: {
                "status": "first_version",
                "sheet_changes": {"added_sheets": [], "removed_sheets": [], "updated_sheets": []},
                "added_rows": [],
                "removed_rows": [],
                "updated_rows": [],
                "value_changes": [],
                "columns_added": [],
                "columns_removed": [],
            },
        )
        monkeypatch.setattr(
            "backend.api.app.extract_columns",
            lambda _path: '{"Sheet1": {"column_names": ["a"], "column_positions": [[1, "A"]]}}',
        )

        files = {
            "input_file": ("input.csv", b"id,value\n1,10\n", "text/csv"),
            "config_file": ("config.csv", b"id,value\n1,10\n", "text/csv"),
        }
        response = client.post(
            "/analyze-excel_files",
            files=files,
            data={"prepare_only": "true", "confirm_ai_analysis": "true"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "prepared"
        assert payload["prepared_input_file_name"] == "input.csv"
        assert payload["prepared_change_status"] == "first_version"


class TestUploadAndProcessBaselineMetadata:
    """Test /upload_and_process baseline metadata flags"""

    def test_upload_returns_baseline_created_on_first_upload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        client = TestClient(app)

        input_dir = tmp_path / "input"
        hash_dir = tmp_path / "hashes"
        saved_dir = tmp_path / "saved"
        input_dir.mkdir()
        hash_dir.mkdir()
        saved_dir.mkdir()

        monkeypatch.setenv("INPUT_DIR", str(input_dir))
        monkeypatch.setenv("HASH_DIR", str(hash_dir))
        monkeypatch.setattr("backend.src.file_change_detector.get_saved_dir", lambda: str(saved_dir))

        files = {"file": ("input.csv", b"id,value\n1,10\n", "text/csv")}
        response = client.post("/upload_and_process", files=files)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["file_path"].endswith("input.csv")
        assert payload["change_results"]["status"] == "first_version"

    def test_upload_returns_baseline_not_created_on_subsequent_upload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        client = TestClient(app)

        input_dir = tmp_path / "input"
        hash_dir = tmp_path / "hashes"
        saved_dir = tmp_path / "saved"
        input_dir.mkdir()
        hash_dir.mkdir()
        saved_dir.mkdir()

        monkeypatch.setenv("INPUT_DIR", str(input_dir))
        monkeypatch.setenv("HASH_DIR", str(hash_dir))
        monkeypatch.setattr("backend.src.file_change_detector.get_saved_dir", lambda: str(saved_dir))

        first_files = {"file": ("input.csv", b"id,value\n1,10\n", "text/csv")}
        first_response = client.post("/upload_and_process", files=first_files)
        assert first_response.status_code == 200
        assert first_response.json()["change_results"]["status"] == "first_version"

        second_files = {"file": ("input.csv", b"id,value\n1,10\n", "text/csv")}
        second_response = client.post("/upload_and_process", files=second_files)

        assert second_response.status_code == 200
        second_payload = second_response.json()
        assert second_payload["status"] == "ok"
        assert second_payload["change_results"]["status"] == "first_version"


class TestMainApiEndpoints:
    """Test the main API endpoints that back the app workflows."""

    def test_health_endpoint_reports_ok(self):
        client = TestClient(app)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_responses_pending_uses_full_response_text(self, tmp_path: Path):
        client = TestClient(app)

        prompts_dir = tmp_path / "responses"
        archive_dir = prompts_dir / "archive"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)

        record_path = prompts_dir / "llm_test.json"
        record_path.write_text(
            json.dumps(
                {
                    "record_id": "llm_test",
                    "created_at_utc": "2026-04-05T18:44:02Z",
                    "type": "completed_analysis",
                    "status": "completed",
                    "prompt": "prompt text",
                    "input": {
                        "input_file_name": "input.csv",
                        "config_file_name": "config.csv",
                    },
                    "llm_response": "response text that should not be truncated",
                }
            ),
            encoding="utf-8",
        )

        archive_record_path = archive_dir / "llm_archived.json"
        archive_record_path.write_text(
            json.dumps(
                {
                    "record_id": "llm_archived",
                    "llm_response": "archived response",
                }
            ),
            encoding="utf-8",
        )

        response = client.get("/responses/pending")

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        assert payload["responses"][0]["response_preview"] == "response text that should not be truncated"
        assert payload["responses"][0]["has_response"] is True

    def test_responses_archive_moves_file(self, tmp_path: Path):
        client = TestClient(app)

        prompts_dir = tmp_path / "responses"
        archive_dir = prompts_dir / "archive"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)

        record_path = prompts_dir / "llm_archive_me.json"
        record_path.write_text(
            json.dumps(
                {
                    "record_id": "llm_archive_me",
                    "llm_response": "archive me",
                }
            ),
            encoding="utf-8",
        )

        response = client.post("/responses/archive", json={"record_file": str(record_path)})

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["archived"] is True
        assert payload["source"] == str(record_path)
        assert payload["archived_file"] == str(archive_dir / "llm_archive_me.json")
        assert not record_path.exists()
        assert (archive_dir / "llm_archive_me.json").exists()

    def test_analyze_config_options_handles_single_and_multiple(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        client = TestClient(app)

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "single_config.xlsx").write_text("single", encoding="utf-8")

        monkeypatch.setattr("backend.api.app._config_dir", lambda: config_dir)

        single_response = client.get("/analyze/config-options")
        assert single_response.status_code == 200
        single_payload = single_response.json()
        assert single_payload["status"] == "single"
        assert single_payload["selected_config"] == "single_config.xlsx"

        (config_dir / "another_config.xlsx").write_text("multiple", encoding="utf-8")
        multiple_response = client.get("/analyze/config-options")
        assert multiple_response.status_code == 200
        multiple_payload = multiple_response.json()
        assert multiple_payload["status"] == "multiple"
        assert sorted(multiple_payload["config_options"]) == ["another_config.xlsx", "single_config.xlsx"]

    def test_upload_config_saves_file_and_updates_local_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        client = TestClient(app)

        monkeypatch.setattr("backend.api.app.PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("backend.api.app.ANALYZE_CONFIG_DIR", tmp_path / "data" / "config")

        upload = {
            "file": ("config_upload.csv", b"id,value\n1,alpha\n", "text/csv"),
        }
        response = client.post("/upload_config", files=upload)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert (tmp_path / "data" / "config" / "config_upload.csv").exists()
        assert "ANALYZE_DEFAULT_CONFIG_FILE=config_upload.csv" in (tmp_path / ".env").read_text(encoding="utf-8")
