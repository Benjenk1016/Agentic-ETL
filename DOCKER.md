lsof -i :8080  # Mac/Linux
# Docker Quick Reference

## Basic Docker Commands

- **Start all containers (build if needed):**
  ```bash
  docker compose up --build
  ```
- **Start in background:**
  ```bash
  docker compose up -d
  ```
- **Stop all containers:**
  ```bash
  docker compose down
  ```
- **Rebuild without running:**
  ```bash
  docker compose build
  ```
- **View logs (web):**
  ```bash
  docker compose logs -f web
  ```
- **View logs (api):**
  ```bash
  docker compose logs -f api
  ```

## Testing

- **Run backend tests (from project root):**
  ```bash
  docker compose exec api pytest backend/src/tests
  ```
  (Or run `pytest backend/src/tests` locally if not using Docker)

## File Structure (Key Files/Folders)

- **Dockerfile** — Main Docker build for ETL or API
- **docker/Dockerfile.web** — Web UI container (Nginx)
- **docker/Dockerfile.api** — FastAPI backend container
- **docker-compose.yml** — Orchestrates all services
- **frontend/** — UI (HTML, CSS, JS, Nginx config)
- **backend/** — Python code, API, and tests
  - **backend/api/** — FastAPI app and related modules
  - **backend/src/** — ETL, file processing, and utility scripts
  - **backend/src/tests/** — Pytest test suite
  - **backend/requirements.txt** — Python dependencies
- **data/** — Input/output files for ETL
  - **data/archive/** — Baseline and historical snapshots
  - **data/state/** — Hashes, manifests, sync state
  - **data/input/** — New files to process
  - **data/config/** — Config files for processing rules
  - **data/responses/** — Saved LLM response records for review


## Troubleshooting

### "Cannot connect to Docker daemon"
- Ensure Docker Desktop is running:
  - **Windows**: Check system tray for Docker icon
  - **Mac**: Check menu bar for Docker icon
  - **Linux**: `sudo systemctl start docker`

### Changes Not Reflected After Code Edits
```bash
# Force rebuild without using cache
docker compose build --no-cache
# Start containers
docker compose up -d
```

### API or Web Container Returns Errors
```bash
# View recent logs
docker compose logs api --tail=50
docker compose logs web --tail=50
# Restart just the API or web container
docker compose restart api
docker compose restart web
# Full restart
docker compose down
docker compose up -d
```

### Port Already in Use
If you see "Address already in use" for port 8080:
```bash
# Find process using port 8080
netstat -ano | findstr :8080  # Windows
lsof -i :8080  # Mac/Linux
# Kill that process or change port in docker-compose.yml
```
To change the port, edit `docker-compose.yml` and modify the web service:
```yaml
services:
  web:
    ports:
      - "3000:80"  # Access at http://localhost:3000 instead
```

### Container Won't Start or Exited Immediately
```bash
# View full error logs
docker compose logs web
# Check running containers
docker compose ps
# Inspect specific service
docker compose logs -f api
```

### Clear Everything and Start Fresh
```bash
# Stop all containers
docker compose down
# Remove all unused images and containers
docker system prune -a
# Rebuild and restart
docker compose up --build -d
```

### Out of Memory
```bash
# Check Docker resource limits (Docker Desktop Settings → Resources)
# View container memory usage
docker stats
```
For low-memory systems, consider:
- Running fewer services at once
- Increasing Docker's memory allocation

---
For more details, see README.md or DOCUMENTATION.md.


# Extra details from README.md (I wasn't sure where to put it, and it is still helpful for understanding the context of the project)


### Change Detection Notes (Current Behavior)

- The upload flow tracks baseline versions using saved files and hashes.
- On first upload of a filename, a baseline is created and the UI reports first-version creation.
- On later uploads with the same filename, the UI reports a human-readable change summary (sheets/rows/cells) when differences exist.
- When a file is a new baseline, the UI keeps the impact label but hides the zero-diff change summary block.
- Numeric formatting-only differences (for example `5.0` vs `5`) are normalized and ignored by change detection.

Right now, it detects changes and outputs them into the chat. Meaningful change details appear before the LLM run, while new-file cases skip the empty summary block.


## AI Analysis with Ollama

-"Analyze" is NOT the end product, it is just a way to test and demonstrate how the AI can analyze files and provide useful information that can be used in the transformation process. 
-This feature allows you to upload an input file and a config file, and then uses a local AI model (Ollama) to analyze the contents of both files. The AI provides insights such as column names, data types, patterns, and potential issues. It shares logic with the file processing command, which is the more advanced feature that serves as the main product.
- The real product is in the "run file processing" command, which automatically detects changes and then uses the AI to decide what to do with the changed files (transform, ask for human input, etc).

The run file processing flow now uses a two-phase prepare/execution path so the chat can show prompt preview and warmup status before the model finishes. 

In run file processing, LLM response records are stored in `data/responses/` and reviewed items are moved to `data/responses/archive/`. Cleanup clears both folders.

Those responses can then be used in the new "process responses" command, which allows the user to read the LLM response and decide how to handle the suggested changes. If they choose to handle it with code, then the code previews all updated spreadsheet entries in the config file, and the user can choose to accept or deny each change using a checkbox, and the config file will only be updated with the accepted changes.


## OneDrive Account Management

To switch accounts:
```bash
rm ./data/state/token_cache.bin
python -m backend.src.onedrive_download
docker compose up --build -d web api
```
Or delete data/state/token_cache.bin manually and use the chat UI commands: `authenticate onedrive` and `complete onedrive auth`.

See [DOCKER.md](DOCKER.md) for advanced CLI usage.