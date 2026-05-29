@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo ========================================
echo Agentic ETL Consumer Startup
echo ========================================
echo.

set "OLLAMA_MODEL_NAME=qwen2.5:3b"
set "WEB_IMAGE=benjenk/agentic-web:0.0.2"
set "API_IMAGE=benjenk/agentic-api:0.0.2"
set "ENV_FILE=config\.env"
set "ENV_TEMPLATE=config\.env.example"
set "COMPOSE_FILE=config\docker-compose.yml"

echo [1/8] Checking Docker installation...
docker --version >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop is not installed.
    echo Opening the Docker download page...
    start "" "https://www.docker.com/products/docker-desktop/"
    echo.
    echo Install Docker Desktop, then run this file again.
    pause
    exit /b 1
)

echo [2/8] Checking Docker Desktop is running...
docker info >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop is installed, but it is not running yet.
    echo Open Docker Desktop and wait until it says it is running.
    pause
    exit /b 1
)

echo [3/8] Checking environment file...
if not exist "config" mkdir "config"
if exist ".env" if not exist "%ENV_FILE%" (
    move /y ".env" "%ENV_FILE%" >nul
    echo Moved legacy .env to %ENV_FILE%.
)
if exist ".env.example" if not exist "%ENV_TEMPLATE%" (
    move /y ".env.example" "%ENV_TEMPLATE%" >nul
    echo Moved legacy .env.example to %ENV_TEMPLATE%.
)

if not exist "%ENV_FILE%" (
    if exist "%ENV_TEMPLATE%" (
        copy /y "%ENV_TEMPLATE%" "%ENV_FILE%" >nul
        echo Created %ENV_FILE% from %ENV_TEMPLATE%.
    ) else (
        echo %ENV_TEMPLATE% was not found.
        pause
        exit /b 1
    )
) else (
    echo %ENV_FILE% already exists.
)

for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b "OLLAMA_MODEL_NAME=" "%ENV_FILE%"`) do (
    if not "%%B"=="" set "OLLAMA_MODEL_NAME=%%B"
)
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b "WEB_IMAGE=" "%ENV_FILE%"`) do (
    if not "%%B"=="" set "WEB_IMAGE=%%B"
)
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b "API_IMAGE=" "%ENV_FILE%"`) do (
    if not "%%B"=="" set "API_IMAGE=%%B"
)

echo Using Ollama model: %OLLAMA_MODEL_NAME%
echo Using web image: %WEB_IMAGE%
echo Using api image: %API_IMAGE%

echo [4/8] Checking Ollama installation...
where ollama >nul 2>&1
if errorlevel 1 (
    echo Ollama is not installed.
    echo Opening the Ollama download page...
    start "" "https://ollama.com/download"
    echo.
    echo Install Ollama, then run this file again.
    pause
    exit /b 1
)

echo [5/8] Checking Ollama service...
set "OLLAMA_OK=0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:11434/api/tags' -TimeoutSec 5; exit 0 } catch { exit 1 }"
if not errorlevel 1 set "OLLAMA_OK=1"
if "%OLLAMA_OK%"=="0" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5; exit 0 } catch { exit 1 }"
    if not errorlevel 1 set "OLLAMA_OK=1"
)
if "%OLLAMA_OK%"=="0" (
    echo Ollama is installed, but its local API did not respond on port 11434.
    echo.
    echo Checked endpoints:
    echo   - http://localhost:11434/api/tags
    echo   - http://127.0.0.1:11434/api/tags
    echo.
    echo Check these items:
    echo   1. Open the Ollama desktop app and wait for it to finish starting.
    echo   2. In a new terminal, run: ollama list
    echo   3. In a browser, open: http://localhost:11434/api/tags
    echo   4. If it still fails, restart Ollama or reboot Windows.
    echo   5. If needed, check whether firewall, VPN, or proxy software is blocking localhost.
    echo.
    echo Current port 11434 listeners:
    netstat -ano | findstr ":11434"
    if errorlevel 1 echo   None found.
    pause
    exit /b 1
)

echo [6/8] Checking Ollama model...
ollama list | findstr /i /c:"%OLLAMA_MODEL_NAME%" >nul
if errorlevel 1 (
    echo The model "%OLLAMA_MODEL_NAME%" is not installed.
    echo.
    echo Run this command first:
    echo   ollama pull %OLLAMA_MODEL_NAME%
    echo.
    echo After the model finishes downloading, run this file again.
    pause
    exit /b 1
)

echo [7/8] Removing old containers with fixed names (if any)...
for %%C in (agentic-etl-web agentic-etl-api) do (
    docker inspect %%C >nul 2>&1
    if not errorlevel 1 (
        echo Removing existing container %%C...
        docker rm -f %%C >nul 2>&1
    )
)

echo [8/8] Checking Docker images...
set "IMAGES_PRESENT=1"
docker image inspect "%WEB_IMAGE%" >nul 2>&1
if errorlevel 1 set "IMAGES_PRESENT=0"
docker image inspect "%API_IMAGE%" >nul 2>&1
if errorlevel 1 set "IMAGES_PRESENT=0"

if "%IMAGES_PRESENT%"=="1" (
    echo Docker images already exist locally. Skipping pull.
) else (
    echo One or more Docker images are missing locally. Pulling published images...
    docker compose -f "%COMPOSE_FILE%" --env-file "%ENV_FILE%" pull web api
    if errorlevel 1 (
        echo Failed to pull the Docker images.
        echo Check your internet connection and verify the image names in %ENV_FILE% if needed.
        pause
        exit /b 1
    )
)

echo.
echo  Starting the web app...
docker compose -f "%COMPOSE_FILE%" --env-file "%ENV_FILE%" up -d web api
if errorlevel 1 (
    echo Failed to start the containers.
    pause
    exit /b 1
)

echo.
echo Waiting for the web app to respond...
set "APP_READY=0"
for /l %%I in (1,1,20) do (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8080' -TimeoutSec 5 ^| Out-Null; exit 0 } catch { exit 1 }"
    if not errorlevel 1 (
        set "APP_READY=1"
        goto :app_ready
    )
    timeout /t 2 /nobreak >nul
)

:app_ready
echo.
if "%APP_READY%"=="1" (
    echo The web app is ready.
) else (
    echo The app is still starting, but the browser will open now.
)

echo Opening Agentic ETL in your browser...
start "" "http://localhost:8080"

echo.
echo Startup complete.
exit /b 0
















      