@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo ========================================
echo Agentic ETL Consumer Shutdown
echo ========================================
echo.

docker --version >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop is not installed.
    pause
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop is not running.
    echo Start Docker Desktop if you want to stop the containers from this script.
    pause
    exit /b 1
)

echo Stopping Agentic ETL containers...
if exist "config\.env" (
    docker compose -f "config\docker-compose.yml" --env-file "config\.env" down
) else (
    docker compose -f "config\docker-compose.yml" down
)

if errorlevel 1 (
    echo Failed to stop the containers.
    pause
    exit /b 1
)

echo.
echo Agentic ETL has been stopped.
pause