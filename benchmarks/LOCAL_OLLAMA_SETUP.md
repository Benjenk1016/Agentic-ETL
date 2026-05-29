# Running Ollama Locally (No Docker)

This guide explains how to run Ollama on your machine instead of in Docker. This approach is **much faster** for benchmarking and testing since it eliminates Docker container overhead.

## Setup

### 1. Download Ollama

1. Download Ollama from: https://ollama.ai/download
2. Install it on your Windows machine
3. After installation, Ollama will start automatically and listen on `http://localhost:11434`

### 2. Pull Models

Open PowerShell or Command Prompt and manually pull the default model:

```bash
ollama pull tinyllama:1.1b

# Optional: pull additional models for comparison
# ollama pull phi:2.7b
# ollama pull llama3.2:1b
```

**Note**: The first time you pull a model, it will download it. Subsequent runs will use the cached version, which is fast.

### 3. Run the Application

Your app and benchmarks are now configured to connect to your local Ollama instance at `http://localhost:11434`.

#### Option A: Run with Docker (App in Docker, Ollama Local)

```bash
docker compose up --build -d api
```

The API container will now connect to your local Ollama via `http://localhost:11434`.

#### Option B: Run Everything Locally (No Docker)

If you want to run the entire app locally:

```bash
cd backend/api
python -m uvicorn app:app --reload --port 5000
```

Then visit `http://localhost:5000` in your browser.

### 4. Run Benchmarks

```bash
python benchmarks/benchmark.py
```

The script will:
1. Check that Ollama is running at `localhost:11434`
2. Automatically discover all installed models
3. Build a fixed prompt suite (`easy`, `medium`, `difficult`) with config from `data/config/`
4. Enrich the difficult prompt with snapshots from `data/input/`
5. Benchmark each model sequentially across the 3 prompt levels
6. Monitor RAM usage during inference (requires `psutil`)
7. Generate a timestamped report with per-prompt times and full outputs: `benchmarks/benchmark_report_YYYYMMDD_HHMMSS.md`

**See [benchmarks/README.md](benchmarks/README.md) for detailed benchmark documentation.**

## Troubleshooting

### Ollama Not Running

If you see "Ollama is not running!" or cannot connect:

```bash
# Verify Ollama is installed
ollama --version

# Start Ollama service (usually auto-starts on boot)
ollama serve

# Test connectivity
curl http://localhost:11434/api/tags
```

Make sure:
1. You installed Ollama from https://ollama.ai/download
2. Ollama is running (check Windows system tray or run `ollama serve`)
3. The app can reach `http://localhost:11434`

### Model Not Found or Analysis Fails

```bash
# List installed models
ollama list

# Pull the model specified in .env
ollama pull tinyllama:1.1b

# Verify .env has correct model name
cat .env | grep OLLAMA_MODEL_NAME  # Linux/Mac
Select-String "OLLAMA_MODEL_NAME" .env  # Windows PowerShell
```

### Out of Memory Errors

If analysis fails with out-of-memory errors:

1. **Use a smaller model** (edit `OLLAMA_MODEL_NAME` in .env):
   - `tinyllama:1.1b` - Recommended (~600MB, works on 4GB RAM systems)
   - `phi:2.7b` - Better quality (~1.6GB, needs 8GB+ RAM)
   - `llama3.2:1b` - Good balance (~1.3GB, works on 8GB RAM systems)

2. **Free up system RAM**:
   - Close Chrome, Firefox, VS Code, and other heavy applications
   - Check available memory: `systeminfo | findstr "Available Physical Memory"` (Windows)

3. **Restart Ollama**:
   ```bash
   ollama serve
   ```

### Slow Analysis or Timeouts

- **First run is slower**: Model must load into memory (can take 30-60 seconds for large models)
- **Subsequent runs are faster**: Model stays cached in RAM
- **Try a smaller/faster model**: tinyllama:1.1b is 10x faster than larger models
- **Check system RAM**: Run `systeminfo` and look for "Available Physical Memory"

### Model Pull Fails or Times Out

```bash
# Pull manually with extended time
ollama pull tinyllama:1.1b

# Check disk space
df -h  # Linux/Mac
Get-PSDrive  # Windows PowerShell
```

If pull still fails:
- Check internet connection
- Try a different model
- Ensure your system has at least 10GB free disk space

### Port Already in Use

If port 11434 is already in use:
1. Stop Ollama: `ollama stop` or kill the process
2. Restart it: `ollama serve`

## Configuration

To use a different Ollama URL, edit these files:

- **API**: `backend/api/llm_prompt.py` - Change `OLLAMA_API_URL`
- **Benchmarks**: `benchmarks/benchmark.py` - Change `OLLAMA_URL`

## Stopping Ollama

To stop Ollama:
- On Windows: Quit from system tray or run `ollama stop`
- The process will keep running until you manually stop it

To keep Ollama running in the background while you work, just leave it running in the system tray.
