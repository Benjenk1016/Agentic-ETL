# Quick Reference: Running the Benchmark

## Prerequisites

1. Install Ollama locally: https://ollama.ai/download
2. Pull at least one model:
   ```powershell
   ollama pull tinyllama:1.1b
   ```
3. Install Python dependencies:
   ```powershell
   pip install requests pandas psutil openpyxl
   ```

## Run

Windows (PowerShell):
```powershell
python .\benchmarks\benchmark.py
```

Windows (Command Prompt):
```cmd
python benchmarks\benchmark.py
```

Linux/Mac:
```bash
python benchmarks/benchmark.py
```

## What The Script Does

1. Checks Ollama at `http://localhost:11434`.
2. Discovers all installed models.
3. Builds 3 prompt levels: easy, medium, difficult.
4. Loads payload sources from:
   - `data/config/` (first sorted file)
   - `data/inputs/` or fallback `data/input/` (first 3 sorted files)
5. Accepts payload sources as:
   - Excel: `.xlsx`, `.xls`, `.xlsm` (converted using `backend/src/column_extract.py`)
   - JSON: `.json` (must already match extract payload shape)
6. Sends the mapping-style prompt to each model for each level.
7. Records timing, estimated throughput, and RAM usage (`psutil`).
8. Writes report: `benchmarks/benchmark_report_YYYYMMDD_HHMMSS.md`.

## JSON Payload Format

JSON inputs in `data/config/` or `data/input(s)/` must match this structure:

```json
{
  "sheet 1 (Sheet1)": {
    "column_names": ["Client Name", "Amount"],
    "column_positions": [["A", 1], ["B", 1]]
  }
}
```

## Common Checks

No models found:
```powershell
ollama list
ollama pull tinyllama:1.1b
```

No test files found:
```powershell
ls data\config
ls data\inputs
ls data\input
```

RAM tracking missing:
```powershell
pip install psutil
```

## Related Docs

- [README.md](README.md)
- [TEST_CASES.md](TEST_CASES.md)
- [LOCAL_OLLAMA_SETUP.md](LOCAL_OLLAMA_SETUP.md)
