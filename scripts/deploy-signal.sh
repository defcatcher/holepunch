#!/usr/bin/env bash
# =============================================================================
# scripts/deploy-signal.sh
#
# Deploy the HolePunch signal-server to Google Cloud Run.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated  (gcloud auth login)
#   - GCP project ID exported as GCP_PROJECT_ID
#   - Docker (used by Cloud Build — no local daemon needed)
#
# Usage:
#   export GCP_PROJECT_ID=my-gcp-project
#   ./scripts/deploy-signal.sh
#
# Optional overrides (export before running):
#   GCP_REGION      — Cloud Run region   (default: us-central1)
#   SERVICE_NAME    — Cloud Run service  (default: holepunch-signal)
# =============================================================================

set -euo pipefail

# ── Resolve configuration ─────────────────────────────────────────────────────

PROJECT_ID="${GCP_PROJECT_ID:?Please export GCP_PROJECT_ID=<your-gcp-project-id>}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-holepunch-signal}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_CONTEXT="${REPO_ROOT}/core"

# ── Pretty helpers ────────────────────────────────────────────────────────────

BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[0;33m"
RESET="\033[0m"

step()  { echo -e "\n${CYAN}▶ $*${RESET}"; }
ok()    { echo -e "${GREEN}✔  $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠  $*${RESET}"; }
fatal() { echo -e "\033[0;31m✖  $*${RESET}" >&2; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────────────────

command -v gcloud >/dev/null 2>&1 \
  || fatal "gcloud CLI not found. Install it from https://cloud.google.com/sdk"

[[ -f "${BUILD_CONTEXT}/go.mod" ]] \
  || fatal "Build context not found: ${BUILD_CONTEXT}/go.mod"

[[ -f "${BUILD_CONTEXT}/cmd/signal-server/Dockerfile" ]] \
  || fatal "Dockerfile not found: ${BUILD_CONTEXT}/cmd/signal-server/Dockerfile"

# ── Print deployment plan ─────────────────────────────────────────────────────

echo -e "\n${BOLD}HolePunch Signal-Server — Cloud Run Deployment${RESET}"
echo    "──────────────────────────────────────────────"
echo    "  Project   : ${PROJECT_ID}"
echo    "  Region    : ${REGION}"
echo    "  Service   : ${SERVICE_NAME}"
echo    "  Image     : ${IMAGE}"
echo    "  Context   : ${BUILD_CONTEXT}"
echo    "──────────────────────────────────────────────"

read -r -p $'\nProceed? [y/N] ' confirm
[[ "${confirm,,}" == "y" ]] || { warn "Aborted."; exit 0; }

# ── Enable required APIs (idempotent) ─────────────────────────────────────────

step "Enabling required GCP APIs…"
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  containerregistry.googleapis.com \
  --project "${PROJECT_ID}" \
  --quiet
ok "APIs enabled."

# ── Build & push image via Cloud Build ───────────────────────────────────────
# We pass the Dockerfile path explicitly so Cloud Build can find it inside
# the core/ build context without a top-level Dockerfile.
step "Building Docker image with Cloud Build (this may take ~2 min on first run)…"
cp "${BUILD_CONTEXT}/cmd/signal-server/Dockerfile" "${BUILD_CONTEXT}/Dockerfile"

gcloud builds submit "${BUILD_CONTEXT}" \
  --tag "${IMAGE}" \
  --project "${PROJECT_ID}"
rm "${BUILD_CONTEXT}/Dockerfile"
ok "Image pushed: ${IMAGE}"
# ── Deploy to Cloud Run ───────────────────────────────────────────────────────

step "Deploying to Cloud Run (${REGION})…"
gcloud run deploy "${SERVICE_NAME}" \
  --image          "${IMAGE}"          \
  --platform       managed             \
  --region         "${REGION}"         \
  --allow-unauthenticated              \
  --port           8080                \
  --memory         256Mi               \
  --cpu            1                   \
  --min-instances  0                   \
  --max-instances  20                  \
  --timeout        90s                 \
  --project        "${PROJECT_ID}"     \
  --quiet
ok "Cloud Run service deployed."

# ── Print the public URL ──────────────────────────────────────────────────────

SERVICE_URL="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --platform managed \
    --region   "${REGION}"  \
    --project  "${PROJECT_ID}" \
    --format   "value(status.url)"
)"

echo
echo -e "${BOLD}────────────────────────────────────────────────────────${RESET}"
echo -e "${GREEN}${BOLD}  ✅  Deployment complete!${RESET}"
echo    ""
echo    "  Signal server URL:"
echo -e "  ${BOLD}${SERVICE_URL}${RESET}"
echo    ""
echo    "  Set this in your environment before running main.py:"
echo    ""
echo -e "  ${CYAN}export HOLEPUNCH_SIGNAL_URL=${SERVICE_URL}${RESET}"
echo    ""
echo    "  Or create a .env file:"
echo -e "  ${CYAN}echo 'HOLEPUNCH_SIGNAL_URL=${SERVICE_URL}' > .env${RESET}"
echo -e "${BOLD}────────────────────────────────────────────────────────${RESET}"
echo

# ── Smoke-test the health endpoint ───────────────────────────────────────────

step "Smoke-testing /health endpoint…"
HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${SERVICE_URL}/health")"

if [[ "${HTTP_STATUS}" == "200" ]]; then
  ok "Health check passed (HTTP ${HTTP_STATUS})."
else
  warn "Health check returned HTTP ${HTTP_STATUS} — the service may still be warming up."
  warn "Retry manually: curl ${SERVICE_URL}/health"
fi
