@echo off
REM Model Benchmarking Suite - Windows Batch Script
REM Runs the Python benchmark script with proper Python environment

echo.
echo Starting Model Benchmark Suite...
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH
    echo Please install Python or add it to your PATH
    exit /b 1
)

REM Run the benchmark
echo Running benchmarks (this will take 30-60 minutes)...
echo.
python benchmarks\benchmark.py

if errorlevel 1 (
    echo.
    echo Error running benchmark. Check the output above.
    pause
    exit /b 1
)

echo.
echo Benchmark complete! Check the generated report.
pause
