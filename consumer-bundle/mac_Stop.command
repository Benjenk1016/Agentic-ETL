#!/bin/bash
cd "$(dirname "$0")" || exit 1

echo "========================================"
echo " Agentic ETL Shutdown"
echo "========================================"
echo ""

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed on this machine."
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose is not available."
    exit 1
fi

echo "Stopping Agentic ETL containers..."
if [ -f "config/.env" ]; then
    docker compose -f "config/docker-compose.yml" --env-file "config/.env" down --remove-orphans
else
    docker compose -f "config/docker-compose.yml" down --remove-orphans
fi

if [ $? -eq 0 ]; then
    echo "Shutdown complete."
else
    echo "Docker Compose reported an error while stopping the app."
    exit 1
fi
