# Benchmark Test Cases

This document explains how benchmark test cases are selected and how to prepare Excel or JSON payload inputs for model benchmarking.

## Overview

The benchmark runs a fixed 3-level progression (`easy`, `medium`, `difficult`) for every model.

Each prompt compares one INPUT payload with one CONFIG payload, then asks the model to produce column mapping suggestions.

## How Files Are Chosen

1. Config source directory is `data/config/`.
2. Input source directory is auto-detected in this order:
   - `data/inputs/`
   - `data/input/`
3. Supported source files in both directories:
   - `.xlsx`, `.xls`, `.xlsm`, `.json`
4. Config source used: first sorted file in `data/config/`.
5. Input sources used: first 3 sorted files in input directory.

If fewer than 3 input files exist, the benchmark repeats files until it fills easy/medium/difficult.

## Accepted Source Types

### Excel Sources

Excel files are processed through `backend/src/column_extract.py` to produce payloads.

### JSON Sources

JSON files must already be in extract-columns payload shape.

Example:

```json
{
  "sheet 1 (Sheet1)": {
    "column_names": ["Customer", "Amount"],
    "column_positions": [["A", 1], ["B", 2]]
  },
  "sheet 2 (Sheet2)": {
    "column_names": ["Region"],
    "column_positions": [["A", 1]]
  }
}
```

## Prompt Template Used

The benchmark uses the same mapping-style prompt intent as the analyze endpoint.

It includes:

- INPUT payload (formatted)
- CONFIG payload (formatted)
- Automated comparison starter:
  - overlap ratio
  - shared columns
  - missing in input
  - extra in input

It asks the model to:

1. Suggest INPUT-to-CONFIG mappings.
2. Identify INPUT columns with no clear match.
3. Output mapping lines in a strict style.

Rules enforced in prompt text:

- Every INPUT column must be listed.
- Every CONFIG column must be accounted for.
- Ambiguities must be flagged explicitly.

## Building Good Test Cases

Use cases that reflect real ETL pain points:

- Header typos (`Ammount` vs `Amount`)
- Synonyms (`Cust Name` vs `Customer Name`)
- Missing expected columns
- Extra unexpected columns
- Multi-sheet workbook mismatch patterns

## Add New Cases

Add Excel or JSON files into input directory:

```powershell
copy .\my_case.xlsx .\data\input\
copy .\my_case_payload.json .\data\input\
python benchmarks\benchmark.py
```

Add/replace config source:

```powershell
copy .\my_config.xlsx .\data\config\
copy .\my_config_payload.json .\data\config\
```

Tip: because selection is sorted-file based, rename files to control ordering.

## Troubleshooting

No config source found:
```powershell
ls data\config
```

No input source found:
```powershell
ls data\inputs
ls data\input
```

JSON rejected:

- Ensure top-level JSON value is an object/dictionary.
- Ensure each sheet entry includes `column_names` list and `column_positions` list.

## Practical Workflow

1. Create one trusted config payload (Excel or JSON).
2. Add 3 representative input cases (Excel and/or JSON).
3. Run benchmark.
4. Review report sections:
   - `Prompts Used (Exact)`
   - `Detailed Results`
   - `Per-Prompt Timing Matrix`
5. Choose the model that balances output quality and runtime.
1. Change config selection from first file to a named file near benchmark.py:211.
2. Replace input selection logic near benchmark.py:214 with an explicit allow-list.