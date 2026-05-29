# Testing Guide

## Automated Test Coverage

The backend test suite covers:
- Change detection and confirmation workflows
- Per-sheet and row-level change detection
- Baseline-save safety (baseline is not updated if user does not confirm)
- Upload baseline metadata (first upload returns `baseline_created=true`, subsequent same-content upload returns `baseline_created=false`)
- Numeric normalization in change detection (format-only differences like `5.0` vs `5` are ignored)

## Running Tests

### Run All Tests
```bash
docker compose exec api pytest backend/src/tests
```

### Run Targeted Tests
```bash
docker compose exec api pytest backend/src/tests/test_analysis.py backend/src/tests/test_validate_excel_files.py backend/src/tests/test_etl_utils.py -q
```

### Run Only Analyze/Workflow Tests
```bash
docker compose exec api pytest backend/src/tests/test_analysis.py -q
```

---

## Benchmarking with Ollama

We have Ollama benchmark scripts and previous runs saved in the '/benchmarks' directory. If you wish to run your own benchmarks, you may follow the README.md and QUICK_START.md files in that directory. Note that the benchmark runs using local python. While there exists a separate docker file in there, it is recommended to use the python commands or the button, as the docker file is not fully set up and may require additional configuration to run properly.

Benchmarking was originally only intended for a quick local test, and Logan made it with the intention of running our real prompt through many models and seeing which was fastest and most correct. But it proved to be a useful tool for testing prompt variations and seeing how they performed across different models, so we kept it around. Keep in mind that it was not meant for client use, so it is not as polished as the rest of the codebase. 

You can manually edit the benchmark script LLM prompt starting on line 309 of `benchmark_script.py` to test different prompt variations.