#!/bin/bash
cd "$(dirname "$0")" || exit 1

pause_and_exit() {
    echo ""
    read -p "Press Enter to exit."
    exit "${1:-1}"
}

set -u

echo "========================================"
echo " Agentic ETL Consumer Startup"
echo "========================================"
echo ""

WEB_IMAGE="benjenk/agentic-end-to-end-web:v0.1"
API_IMAGE="benjenk/agentic-end-to-end-api:v0.1"
OLLAMA_MODEL_NAME="qwen2.5:3b"
ENV_FILE="config/.env"
ENV_TEMPLATE="config/.env.example"
COMPOSE_FILE="config/docker-compose.yml"

echo "[1/8] Checking Docker installation..."
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Desktop is not installed."
    echo "Opening the Docker download page..."
    open "https://www.docker.com/products/docker-desktop/"
    echo "Install Docker Desktop, then run this file again."
    pause_and_exit 1
fi

echo "[2/8] Checking Docker Desktop is running..."
if ! docker info >/dev/null 2>&1; then
    echo "Docker Desktop is installed, but it is not running yet."
    echo "Open Docker Desktop and wait until it says it is running."
    pause_and_exit 1
fi

echo "[3/8] Checking environment file..."
mkdir -p "config"
if [ -f ".env" ] && [ ! -f "$ENV_FILE" ]; then
    mv ".env" "$ENV_FILE"
    echo "Moved legacy .env to $ENV_FILE."
fi
if [ -f ".env.example" ] && [ ! -f "$ENV_TEMPLATE" ]; then
    mv ".env.example" "$ENV_TEMPLATE"
    echo "Moved legacy .env.example to $ENV_TEMPLATE."
fi

if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_TEMPLATE" ]; then
        cp "$ENV_TEMPLATE" "$ENV_FILE"
        echo "Created $ENV_FILE from $ENV_TEMPLATE."
    else
        echo "$ENV_TEMPLATE was not found."
        pause_and_exit 1
    fi
else
    echo "$ENV_FILE already exists."
fi

ENV_OLLAMA_MODEL=$(grep -E "^OLLAMA_MODEL_NAME=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
ENV_WEB_IMAGE=$(grep -E "^WEB_IMAGE=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
ENV_API_IMAGE=$(grep -E "^API_IMAGE=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)

if [ -n "${ENV_OLLAMA_MODEL:-}" ]; then
    OLLAMA_MODEL_NAME="$ENV_OLLAMA_MODEL"
fi
if [ -n "${ENV_WEB_IMAGE:-}" ]; then
    WEB_IMAGE="$ENV_WEB_IMAGE"
fi
if [ -n "${ENV_API_IMAGE:-}" ]; then
    API_IMAGE="$ENV_API_IMAGE"
fi

echo "Using Ollama model: $OLLAMA_MODEL_NAME"
echo "Using web image: $WEB_IMAGE"
echo "Using api image: $API_IMAGE"

echo "[4/8] Checking Ollama..."
if ! curl -s --max-time 5 "http://localhost:11434/api/tags" >/dev/null 2>&1; then
    echo "Ollama is installed, but its local API did not respond on port 11434."
    echo "Open the Ollama desktop app and wait for it to finish starting, then run this file again."
    echo "You can download it from: https://ollama.com/download"
    pause_and_exit 1
fi

if command -v ollama >/dev/null 2>&1; then
    echo "[5/8] Checking Ollama model..."
    if ! ollama list 2>/dev/null | grep -Fqi "$OLLAMA_MODEL_NAME"; then
        echo "The model '$OLLAMA_MODEL_NAME' is not installed."
        echo "Downloading now (this may take a few minutes)..."
        if ! ollama pull "$OLLAMA_MODEL_NAME"; then
            echo "Failed to download model '$OLLAMA_MODEL_NAME'."
            pause_and_exit 1
        fi
    fi
else
    echo "[5/8] Checking Ollama model..."
    echo "Warning: Ollama server is reachable, but the 'ollama' CLI was not found."
    echo "Skipping model verification and continuing."
fi

echo "[6/8] Removing old containers with fixed names (if any)..."
for container in agentic-etl-web agentic-etl-api; do
    if docker inspect "$container" >/dev/null 2>&1; then
        echo "Removing existing container $container..."
        docker rm -f "$container" >/dev/null 2>&1
    fi
done

echo "[7/8] Checking Docker images..."
IMAGES_PRESENT=1
if ! docker image inspect "$WEB_IMAGE" >/dev/null 2>&1; then
    IMAGES_PRESENT=0
fi
if ! docker image inspect "$API_IMAGE" >/dev/null 2>&1; then
    IMAGES_PRESENT=0
fi

if [ "$IMAGES_PRESENT" -eq 1 ]; then
    echo "Docker images already exist locally. Skipping pull."
else
    echo "One or more Docker images are missing locally. Pulling published images..."
    if ! docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull web api; then
        echo "Failed to pull the Docker images."
        echo "Check your internet connection and verify image names in $ENV_FILE if needed."
        pause_and_exit 1
    fi
fi

echo "[8/8] Starting containers..."
if ! docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d web api; then
    echo "Failed to start the containers."
    pause_and_exit 1
fi

echo ""
echo "Waiting for the web app to respond..."
APP_READY=0
for _ in $(seq 1 20); do
    if curl -s --max-time 5 "http://localhost:8080" >/dev/null 2>&1; then
        APP_READY=1
        break
    fi
    sleep 2
done

echo ""
if [ "$APP_READY" -eq 1 ]; then
    echo "The web app is ready."
else
    echo "The app is still starting, but the browser will open now."
fi

echo "Opening Agentic ETL in your browser..."
open "http://localhost:8080"

echo ""
echo "Startup complete."
