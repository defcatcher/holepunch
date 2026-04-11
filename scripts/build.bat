@echo off
:: =============================================================================
:: HolePunch — Windows build script
:: Builds the holepunch backend binary for the current machine (amd64).
::
:: Usage:
::   scripts\build.bat
::
:: Output:
::   bin\holepunch-windows-amd64.exe
:: =============================================================================
setlocal EnableDelayedExpansion

set REPO_ROOT=%~dp0..
set BIN_DIR=%REPO_ROOT%\bin
set SRC_DIR=%REPO_ROOT%\core

:: ── Prerequisite check ────────────────────────────────────────────────────────
where go >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Go toolchain not found in PATH.
    echo         Download it from https://go.dev/dl/ and re-run this script.
    exit /b 1
)

for /f "tokens=3" %%v in ('go version') do set GO_VERSION=%%v
echo [INFO]  Go version : %GO_VERSION%

:: ── Output directory ─────────────────────────────────────────────────────────
if not exist "%BIN_DIR%" (
    mkdir "%BIN_DIR%"
    echo [INFO]  Created bin\
)

:: ── Build holepunch ───────────────────────────────────────────────────────────
echo.
echo [....] Building holepunch-windows-amd64.exe ...

cd /d "%SRC_DIR%"

set GOOS=windows
set GOARCH=amd64
set CGO_ENABLED=0

go build -ldflags="-s -w" -o "%BIN_DIR%\holepunch-windows-amd64.exe" .\cmd\holepunch

if %ERRORLEVEL% neq 0 (
    echo [FAIL]  Build failed — check the errors above.
    exit /b 1
)

echo [ OK ] bin\holepunch-windows-amd64.exe

:: ── Build signal-server (optional, Linux target for Cloud Run) ────────────────
echo.
echo [....] Building signal-server-linux-amd64 (Cloud Run target) ...

set GOOS=linux
set GOARCH=amd64

go build -ldflags="-s -w" -o "%BIN_DIR%\signal-server-linux-amd64" .\cmd\signal-server

if %ERRORLEVEL% neq 0 (
    echo [WARN]  signal-server build failed — skip if you are not deploying to Cloud Run.
) else (
    echo [ OK ] bin\signal-server-linux-amd64
)

:: ── Summary ───────────────────────────────────────────────────────────────────
echo.
echo ============================================================
echo  Done!  Artifacts in bin\
echo ============================================================
dir /b "%BIN_DIR%"
echo.

endlocal
