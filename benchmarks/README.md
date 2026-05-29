# Model Benchmarking Suite

This directory benchmarks locally installed Ollama models using the same column-based ETL analysis style as the API flow.

## What It Measures

- Model size (MB)
- Peak and average RAM usage (MB)
- Elapsed time per prompt level (easy/medium/difficult)
- Total time and average prompt time per model
- Estimated throughput (tokens/sec)
- Full model responses for each prompt level

## Prerequisites

1. Install Ollama: https://ollama.ai/download
2. Install Python dependencies:
   ```powershell
   pip install requests pandas psutil openpyxl
   ```
3. Ensure Ollama is running:
   ```powershell
   curl http://localhost:11434/api/tags
   ```

## Run Benchmark

```powershell
python benchmarks\benchmark.py
```

The script auto-discovers installed models with `ollama /api/tags` and benchmarks each one.

## Data Sources Used

The script reads benchmark cases from:

- Config source: first sorted file in `data/config/`
- Input sources: first 3 sorted files in `data/inputs/`; falls back to `data/input/`

Supported file types for both config and input sources:

- Excel: `.xlsx`, `.xls`, `.xlsm`
- JSON: `.json`

Excel files are converted to payloads using `backend/src/column_extract.py`.
JSON files must already be extract-columns payload JSON.

## Expected JSON Payload Shape

```json
{
  "sheet 1 (Sheet1)": {
    "column_names": ["Column A", "Column B"],
    "column_positions": [["A", 1], ["B", 3]]
  }
}
```

## Prompt Behavior

The benchmark prompt matches the mapping-focused prompt used by the analyze endpoint in `backend/api/app.py`:

- Uses INPUT payload + CONFIG payload + automated shared/missing/extra comparison summary
- Asks for column mapping suggestions
- Requires every INPUT column and every CONFIG column to be accounted for
- Requires ambiguity to be flagged explicitly

## Output

A report is written to:

`benchmarks/benchmark_report_YYYYMMDD_HHMMSS.md`

Report sections include:

- Summary table of all models
- Recommendations (fastest, lowest RAM, efficiency)
- Exact prompts used
- Detailed per-model and per-prompt outputs
- Easy/medium/difficult timing matrix

## Troubleshooting

No models found:
```powershell
ollama list
ollama pull tinyllama:1.1b
```

No benchmark input/config files found:
```powershell
ls data\config
ls data\inputs
ls data\input
```

RAM tracking unavailable:
```powershell
pip install psutil
```

## Related Files

- `benchmark.py`: main benchmark runner
- `pre_benchmark_check.py`: environment checks before benchmarking
- `QUICK_START.md`: concise run guide
- `TEST_CASES.md`: detailed test case and payload guide
- `run_benchmark.bat`: Windows convenience wrapper

**Error Handling:**
- Per-worksheet error isolation (bad Excel sheets don't crash benchmark)
- Per-prompt error tracking (partial failures reported)
- Timeout handling with graceful degradation

## Available Models

Browse models at: https://ollama.com/library

Popular choices:
- **Ultra-lightweight** (<1GB): `tinyllama:1.1b`, `qwen2.5:0.5b`
- **Lightweight** (1-2GB): `llama3.2:1b`, `phi:2.7b`, `gemma:2b`
- **Medium** (2-5GB): `qwen2.5:3b`, `phi3:3.8b`, `mistral:7b`
- **Large** (5GB+): `llama2:7b`, `mistral:7b`, `neural-chat:7b`
