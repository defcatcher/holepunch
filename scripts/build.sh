#!/usr/bin/env bash
# =============================================================================
# scripts/build.sh — Build all HolePunch Go binaries
# =============================================================================
#
# Produces the following outputs in bin/:
#
#   holepunch-windows-amd64.exe    holepunch-windows-arm64.exe
#   holepunch-darwin-amd64         holepunch-darwin-arm64
#   holepunch-linux-amd64          holepunch-linux-arm64
#   signal-server-linux-amd64      (for Cloud Run deployment)
#
# Usage:
#   ./scripts/build.sh             # build everything
#   ./scripts/build.sh windows     # windows binaries only
#   ./scripts/build.sh darwin      # macOS binaries only
#   ./scripts/build.sh linux       # linux binaries only
#   ./scripts/build.sh signal      # signal-server only
#
# Requirements:
#   Go 1.22+  (https://go.dev/dl/)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${REPO_ROOT}/bin"
SRC_DIR="${REPO_ROOT}/core"
FILTER="${1:-all}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RESET='\033[0m'

log()  { echo -e "${CYAN}  ▶${RESET} $*"; }
ok()   { echo -e "${GREEN}  ✓${RESET} $*"; }
warn() { echo -e "${YELLOW}  !${RESET} $*"; }

# build <GOOS> <GOARCH> [suffix]
# Compiles ./cmd/holepunch and writes the binary to BIN_DIR.
build_holepunch() {
    local goos="$1"
    local goarch="$2"
    local suffix="${3:-}"
    local name="holepunch-${goos}-${goarch}${suffix}"
    local out="${BIN_DIR}/${name}"

    log "holepunch  ${goos}/${goarch}  →  bin/${name}"
    (
        cd "${SRC_DIR}"
        GOOS="${goos}" GOARCH="${goarch}" \
            go build -ldflags="-s -w" -trimpath \
            -o "${out}" \
            ./cmd/holepunch
    )
    ok "${name}  ($(du -sh "${out}" | cut -f1))"
}

build_signal() {
    local name="signal-server-linux-amd64"
    local out="${BIN_DIR}/${name}"

    log "signal-server  linux/amd64  →  bin/${name}"
    (
        cd "${SRC_DIR}"
        GOOS=linux GOARCH=amd64 \
            go build -ldflags="-s -w" -trimpath \
            -o "${out}" \
            ./cmd/signal-server
    )
    ok "${name}  ($(du -sh "${out}" | cut -f1))"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
if ! command -v go &>/dev/null; then
    echo "ERROR: 'go' not found in PATH."
    echo "Install Go 1.22+ from https://go.dev/dl/ and re-run."
    exit 1
fi

GO_VERSION=$(go version | awk '{print $3}' | sed 's/go//')
REQUIRED="1.22"
if [[ "$(printf '%s\n' "$REQUIRED" "$GO_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]]; then
    warn "Go ${GO_VERSION} detected; Go ${REQUIRED}+ is recommended."
fi

mkdir -p "${BIN_DIR}"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}════════════════════════════════════════${RESET}"
echo -e "${CYAN}  HolePunch — build  (filter: ${FILTER})${RESET}"
echo -e "${CYAN}════════════════════════════════════════${RESET}"
echo ""

case "${FILTER}" in
    all | windows)
        build_holepunch windows amd64 .exe
        build_holepunch windows arm64 .exe
        ;;& # fall-through only when filter=all

    all | darwin)
        build_holepunch darwin amd64
        build_holepunch darwin arm64
        ;;&

    all | linux)
        build_holepunch linux amd64
        build_holepunch linux arm64
        ;;&

    all | signal)
        echo ""
        build_signal
        ;;

    *)
        echo "Unknown filter '${FILTER}'. Valid options: all windows darwin linux signal"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Done!  Binaries in bin/${RESET}"
echo -e "${GREEN}════════════════════════════════════════${RESET}"
echo ""
ls -lh "${BIN_DIR}/"
