@echo off
REM =============================================================================
REM scripts/deploy-signal.bat
REM
REM Deploy the HolePunch signal-server to Google Cloud Run from Windows.
REM
REM Prerequisites:
REM   - gcloud CLI installed and authenticated  (gcloud auth login)
REM   - GCP_PROJECT_ID environment variable set
REM
REM Usage:
REM   set GCP_PROJECT_ID=my-gcp-project
REM   scripts\deploy-signal.bat
REM
REM Optional overrides (set before running):
REM   GCP_REGION      — Cloud Run region   (default: us-central1)
REM   SERVICE_NAME    — Cloud Run service  (default: holepunch-signal)
REM =============================================================================

setlocal enabledelayedexpansion

REM ── Resolve configuration ─────────────────────────────────────────────────────

if not defined GCP_PROJECT_ID (
    echo Error: GCP_PROJECT_ID environment variable not set
    echo Usage: set GCP_PROJECT_ID=my-gcp-project
    exit /b 1
)

set PROJECT_ID=%GCP_PROJECT_ID%
if not defined GCP_REGION (set REGION=us-central1) else (set REGION=%GCP_REGION%)
if not defined SERVICE_NAME (set SERVICE_NAME=holepunch-signal)
set IMAGE=gcr.io/%PROJECT_ID%/%SERVICE_NAME%:latest

REM Get the repository root (parent of scripts directory)
for %%A in ("%~dp0..") do set REPO_ROOT=%%~fA
set BUILD_CONTEXT=%REPO_ROOT%\core

REM ── Sanity checks ─────────────────────────────────────────────────────────────

echo.
echo Checking prerequisites...

where gcloud >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: gcloud CLI not found
    echo Install from: https://cloud.google.com/sdk
    exit /b 1
)
echo [OK] gcloud CLI found

if not exist "%BUILD_CONTEXT%\go.mod" (
    echo Error: go.mod not found at %BUILD_CONTEXT%\go.mod
    exit /b 1
)
echo [OK] go.mod found

if not exist "%BUILD_CONTEXT%\cmd\signal-server\Dockerfile" (
    echo Error: Dockerfile not found at %BUILD_CONTEXT%\cmd\signal-server\Dockerfile
    exit /b 1
)
echo [OK] Dockerfile found

if not exist "%BUILD_CONTEXT%\cloudbuild.yaml" (
    echo Error: cloudbuild.yaml not found at %BUILD_CONTEXT%\cloudbuild.yaml
    exit /b 1
)
echo [OK] cloudbuild.yaml found

REM ── Print deployment plan ─────────────────────────────────────────────────────

echo.
echo ============================================================
echo  HolePunch Signal-Server — Cloud Run Deployment
echo ============================================================
echo.
echo  Project   : %PROJECT_ID%
echo  Region    : %REGION%
echo  Service   : %SERVICE_NAME%
echo  Image     : %IMAGE%
echo  Context   : %BUILD_CONTEXT%
echo.
echo ============================================================
echo.

set /p confirm="Proceed? [y/N] "
if /i not "%confirm%"=="y" (
    echo Aborted.
    exit /b 0
)

REM ── Enable required APIs (idempotent) ─────────────────────────────────────────

echo.
echo [*] Enabling required GCP APIs...
gcloud services enable ^
    cloudbuild.googleapis.com ^
    run.googleapis.com ^
    containerregistry.googleapis.com ^
    --project %PROJECT_ID% ^
    --quiet

if %errorlevel% neq 0 (
    echo Error: Failed to enable APIs
    exit /b 1
)
echo [OK] APIs enabled

REM ── Build ^& push image via Cloud Build ───────────────────────────────────────

echo.
echo [*] Building Docker image with Cloud Build...
echo     (this may take 2-3 minutes on first run)
echo.

gcloud builds submit "%BUILD_CONTEXT%" ^
    --project %PROJECT_ID%

if %errorlevel% neq 0 (
    echo Error: Failed to build and push image
    exit /b 1
)
echo [OK] Image pushed: %IMAGE%

REM ── Deploy to Cloud Run ───────────────────────────────────────────────────────

echo.
echo [*] Deploying to Cloud Run (%REGION%)...
gcloud run deploy %SERVICE_NAME% ^
    --image %IMAGE% ^
    --platform managed ^
    --region %REGION% ^
    --allow-unauthenticated ^
    --port 8080 ^
    --memory 256Mi ^
    --cpu 1 ^
    --min-instances 0 ^
    --max-instances 20 ^
    --timeout 90s ^
    --project %PROJECT_ID% ^
    --quiet

if %errorlevel% neq 0 (
    echo Error: Failed to deploy to Cloud Run
    exit /b 1
)
echo [OK] Cloud Run service deployed

REM ── Print the public URL ──────────────────────────────────────────────────────

echo.
echo [*] Retrieving service URL...

for /f "delims=" %%A in ('gcloud run services describe %SERVICE_NAME% --platform managed --region %REGION% --project %PROJECT_ID% --format "value(status.url)" 2^>nul') do (
    set SERVICE_URL=%%A
)

echo.
echo ============================================================
echo   ^✅  Deployment complete!
echo ============================================================
echo.
echo   Signal server URL:
echo   %SERVICE_URL%
echo.
echo   Set this in your environment before running main.py:
echo.
echo   set HOLEPUNCH_SIGNAL_URL=%SERVICE_URL%
echo.
echo   Or create a .env file in the project root with:
echo   HOLEPUNCH_SIGNAL_URL=%SERVICE_URL%
echo.
echo ============================================================
echo.

REM ── Smoke-test the health endpoint ───────────────────────────────────────────

echo [*] Smoke-testing /health endpoint...

where curl >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" "%SERVICE_URL%/health" 2^>nul') do (
        set HTTP_STATUS=%%A
    )

    if "!HTTP_STATUS!"=="200" (
        echo [OK] Health check passed (HTTP !HTTP_STATUS!)
    ) else (
        echo [!] Health check returned HTTP !HTTP_STATUS! - service may still be warming up
        echo [!] Retry manually: curl %SERVICE_URL%/health
    )
) else (
    echo [!] curl not found - skipping health check
    echo [!] You can test manually in your browser: %SERVICE_URL%/health
)

echo.
echo Deployment finished!
endlocal
