# syntax=docker/dockerfile:1
# =============================================================================
# HolePunch — signal-server Dockerfile
#
# Multi-stage build:
#   Stage 1 (builder) — compiles the Go binary inside the official Go image.
#   Stage 2 (runtime) — copies only the static binary into a scratch image.
#
# The final image has no shell, no OS, no package manager — just the binary
# and TLS root certificates. This keeps the image under ~10 MiB and reduces
# the attack surface to essentially zero.
#
# Build context must be the core/ directory (contains go.mod / go.sum):
#   docker build -f cmd/signal-server/Dockerfile -t holepunch-signal .
#
# Run locally:
#   docker run --rm -p 8080:8080 holepunch-signal
# =============================================================================

# ── Stage 1: Build ────────────────────────────────────────────────────────────

FROM golang:1.22-alpine AS builder

# Install ca-certificates, tzdata, and file utility for binary verification
RUN apk add --no-cache ca-certificates tzdata file

WORKDIR /build

# Download dependencies first — this layer is cached as long as go.mod/go.sum
# don't change, which dramatically speeds up rebuilds during development.
COPY go.mod go.sum ./
RUN go mod download && go mod verify

# Copy the rest of the source tree.
COPY . .

# Build a fully static binary:
#   CGO_ENABLED=0  — no libc dependency, required for scratch base
#   -s -w          — strip debug info and DWARF (smaller binary)
#   -trimpath      — remove local build paths from the binary (reproducible)
RUN CGO_ENABLED=0 GOOS=linux \
    go build \
      -ldflags="-s -w -extldflags=-static" \
      -trimpath \
      -o /signal-server \
      ./cmd/signal-server

# Verify the binary is truly static (will print "statically linked" or error).
RUN file /signal-server | grep -q "statically linked" || \
    { echo "ERROR: binary is not statically linked"; exit 1; }

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────

FROM scratch

# Copy TLS root certificates so the binary can validate HTTPS connections
# if it ever needs to make outbound calls (future-proofing).
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Copy timezone data so time.LoadLocation works correctly in logs.
COPY --from=builder /usr/share/zoneinfo /usr/share/zoneinfo

# Copy the compiled binary.
COPY --from=builder /signal-server /signal-server

# ── Runtime configuration ──────────────────────────────────────────────────────

# Google Cloud Run injects $PORT automatically. The signal-server binary reads
# it at startup. Default to 8080 for local docker run.
ENV PORT=8080

# Cloud Run requires the container to listen on $PORT.
EXPOSE 8080

# Run as a non-root UID (scratch has no /etc/passwd, so we use a numeric UID).
# UID 65532 is the conventional "nonroot" user used by distroless images.
USER 65532:65532

ENTRYPOINT ["/signal-server"]
