// Command signal-server is the HolePunch WebRTC signaling broker.
//
// It is a stateless HTTP server (suitable for Google Cloud Run) that lets two
// peers exchange SDP offer/answer strings using a shared 6-digit peer code.
//
// API
//
//	POST /signal/{code}/{role}   body: {"sdp":"..."}  → 200 OK
//	GET  /signal/{code}/{role}                         → 200 {"sdp":"..."} (long-poll)
//	GET  /health                                       → 200 "ok"
//
// The GET endpoint holds the connection open for up to 60 seconds waiting for
// the SDP to be published. If the peer never publishes within that window a
// 504 is returned and the client should retry.
//
// Session entries are purged automatically after sessionTTL (10 minutes) by a
// background goroutine, so the server never accumulates stale state even if
// peers crash mid-negotiation.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

const (
	// longPollTimeout is the maximum time the server will hold a GET request
	// open waiting for the remote peer to publish its SDP.
	longPollTimeout = 60 * time.Second

	// sessionTTL is how long a session entry is kept after creation. Entries
	// older than this are removed by the cleanup goroutine regardless of
	// whether negotiation completed.
	sessionTTL = 10 * time.Minute

	// cleanupInterval controls how often the cleanup goroutine runs.
	cleanupInterval = 2 * time.Minute

	// maxBodyBytes caps the POST body size to protect against large payloads.
	// A fully-trickled SDP with many ICE candidates is well under 64 KiB.
	maxBodyBytes = 64 * 1024
)

// ---------------------------------------------------------------------------
// SDP slot — one side of a session
// ---------------------------------------------------------------------------

// sdpSlot holds a single SDP value plus a broadcast channel that is closed
// the moment the value is written. Any number of concurrent GET waiters can
// block on ready and wake up without polling.
type sdpSlot struct {
	value string
	ready chan struct{} // closed exactly once when value is set
	once  sync.Once
}

func newSDPSlot() *sdpSlot {
	return &sdpSlot{ready: make(chan struct{})}
}

// set stores sdp and wakes up all waiters. Subsequent calls are no-ops thanks
// to sync.Once, so duplicate POSTs from a retrying client are safe.
func (s *sdpSlot) set(sdp string) {
	s.once.Do(func() {
		// Write value before closing ready so that any goroutine that reads
		// value after receiving from ready observes the written string.
		s.value = sdp
		close(s.ready)
	})
}

// wait blocks until the SDP is available, ctx is cancelled, or the deadline
// fires. It returns (sdp, nil) on success, ("", context.Canceled /
// context.DeadlineExceeded) on cancellation, or ("", errTimeout) on timeout.
var errTimeout = errors.New("signal: no SDP published within the long-poll window")

func (s *sdpSlot) wait(ctx context.Context) (string, error) {
	timer := time.NewTimer(longPollTimeout)
	defer timer.Stop()

	select {
	case <-s.ready:
		return s.value, nil
	case <-timer.C:
		return "", errTimeout
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// ---------------------------------------------------------------------------
// Session entry — one peer-code negotiation
// ---------------------------------------------------------------------------

// sessionEntry groups the sender and receiver SDP slots for one peer-code.
type sessionEntry struct {
	mu        sync.Mutex
	slots     map[string]*sdpSlot // "sender" | "receiver" → slot
	createdAt time.Time
}

func newSessionEntry() *sessionEntry {
	return &sessionEntry{
		slots:     make(map[string]*sdpSlot, 2),
		createdAt: time.Now(),
	}
}

// slot returns the existing slot for role, or creates and stores a new one.
// Thread-safe.
func (e *sessionEntry) slot(role string) *sdpSlot {
	e.mu.Lock()
	defer e.mu.Unlock()
	if s, ok := e.slots[role]; ok {
		return s
	}
	s := newSDPSlot()
	e.slots[role] = s
	return s
}

// ---------------------------------------------------------------------------
// Global session store
// ---------------------------------------------------------------------------

// sessions maps peer code (string) → *sessionEntry.
// sync.Map is used because reads (GET long-polls) vastly outnumber writes.
var sessions sync.Map

// getOrCreateSession retrieves the session for code or atomically creates a
// new one. LoadOrStore guarantees that concurrent callers for the same code
// all receive the same *sessionEntry.
func getOrCreateSession(code string) *sessionEntry {
	fresh := newSessionEntry()
	actual, _ := sessions.LoadOrStore(code, fresh)
	return actual.(*sessionEntry)
}

// ---------------------------------------------------------------------------
// HTTP handlers
// ---------------------------------------------------------------------------

// postHandler stores the caller's SDP in the session slot for (code, role).
//
//	POST /signal/{code}/{role}
//	Content-Type: application/json
//	Body: {"sdp":"v=0 ..."}
func postHandler(w http.ResponseWriter, r *http.Request) {
	code, role, ok := extractPathParams(r)
	if !ok {
		http.Error(w, "invalid path: expected /signal/{code}/{role}", http.StatusBadRequest)
		return
	}

	// Decode the request body.
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	var body struct {
		SDP string `json:"sdp"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, fmt.Sprintf("invalid JSON body: %s", err), http.StatusBadRequest)
		return
	}
	if body.SDP == "" {
		http.Error(w, `body must contain a non-empty "sdp" field`, http.StatusBadRequest)
		return
	}

	entry := getOrCreateSession(code)
	entry.slot(role).set(body.SDP)

	slog.Info("signal: SDP stored", "code", code, "role", role)
	w.WriteHeader(http.StatusOK)
}

// getHandler long-polls for the SDP published by (code, role).
//
//	GET /signal/{code}/{role}
//	← 200 {"sdp":"v=0 ..."}
//	← 504 if no SDP arrives within longPollTimeout
func getHandler(w http.ResponseWriter, r *http.Request) {
	code, role, ok := extractPathParams(r)
	if !ok {
		http.Error(w, "invalid path: expected /signal/{code}/{role}", http.StatusBadRequest)
		return
	}

	entry := getOrCreateSession(code)
	slot := entry.slot(role)

	sdp, err := slot.wait(r.Context())
	if err != nil {
		if errors.Is(err, errTimeout) {
			http.Error(w, "timeout: no SDP published within 60 s", http.StatusGatewayTimeout)
			return
		}
		// r.Context() was cancelled because the client disconnected.
		// Nothing to write — the connection is gone.
		return
	}

	slog.Info("signal: SDP delivered", "code", code, "role", role)
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(struct {
		SDP string `json:"sdp"`
	}{SDP: sdp}); err != nil {
		slog.Warn("signal: failed to write response", "err", err)
	}
}

// healthHandler serves the GCP / load-balancer health-check endpoint.
func healthHandler(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, "ok")
}

// ---------------------------------------------------------------------------
// Path helper
// ---------------------------------------------------------------------------

// extractPathParams parses {code} and {role} from /signal/{code}/{role}.
// In Go 1.22 these are available directly via r.PathValue; we also validate
// that role is one of the two legal values.
func extractPathParams(r *http.Request) (code, role string, ok bool) {
	// Go 1.22 ServeMux populates PathValue from named wildcards in the pattern.
	code = r.PathValue("code")
	role = r.PathValue("role")

	// Fallback for routers that don't inject PathValue (tests, etc.).
	if code == "" || role == "" {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/"), "/")
		// Expected layout: ["signal", code, role]
		if len(parts) != 3 || parts[0] != "signal" {
			return "", "", false
		}
		code, role = parts[1], parts[2]
	}

	if code == "" || role == "" {
		return "", "", false
	}
	if role != "sender" && role != "receiver" {
		return "", "", false
	}
	return code, role, true
}

// ---------------------------------------------------------------------------
// Session cleanup
// ---------------------------------------------------------------------------

// startCleanup runs a periodic goroutine that removes sessions older than
// sessionTTL. It exits when ctx is cancelled (i.e. on server shutdown).
func startCleanup(ctx context.Context) {
	ticker := time.NewTicker(cleanupInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			removed := 0
			sessions.Range(func(k, v any) bool {
				entry := v.(*sessionEntry)
				entry.mu.Lock()
				age := time.Since(entry.createdAt)
				entry.mu.Unlock()

				if age > sessionTTL {
					sessions.Delete(k)
					removed++
				}
				return true // continue iteration
			})
			if removed > 0 {
				slog.Info("signal: cleaned up expired sessions", "count", removed)
			}

		case <-ctx.Done():
			return
		}
	}
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

func main() {
	portFlag := flag.String("port", "", "TCP port to listen on (overrides $PORT; default 8080)")
	flag.Parse()

	// Resolve listen port: flag > env > default.
	// $PORT is injected automatically by Google Cloud Run.
	listenPort := "8080"
	if p := os.Getenv("PORT"); p != "" {
		listenPort = p
	}
	if *portFlag != "" {
		listenPort = *portFlag
	}
	addr := ":" + listenPort

	// Build router using Go 1.22 method+wildcard patterns.
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", healthHandler)
	mux.HandleFunc("POST /signal/{code}/{role}", postHandler)
	mux.HandleFunc("GET /signal/{code}/{role}", getHandler)

	srv := &http.Server{
		Addr:    addr,
		Handler: mux,
		// ReadTimeout covers reading the request headers + body.
		// Keep it short — POST bodies are tiny JSON blobs.
		ReadTimeout: 10 * time.Second,
		// WriteTimeout must exceed longPollTimeout so that the server can
		// complete a 60-second long-poll before the response is force-closed.
		WriteTimeout: longPollTimeout + 15*time.Second,
		// IdleTimeout recycles keep-alive connections that have gone quiet.
		IdleTimeout: 120 * time.Second,
	}

	// Root context governs both the cleanup goroutine and the HTTP server.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go startCleanup(ctx)

	// ── Graceful shutdown on SIGTERM / SIGINT ────────────────────────────────
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	go func() {
		sig := <-sigCh
		slog.Info("signal-server: received shutdown signal", "signal", sig.String())

		// Cancel the root context so the cleanup goroutine exits.
		cancel()

		// Give in-flight long-polls up to 70 seconds to complete naturally.
		shutCtx, shutCancel := context.WithTimeout(context.Background(), longPollTimeout+10*time.Second)
		defer shutCancel()

		if err := srv.Shutdown(shutCtx); err != nil {
			slog.Error("signal-server: graceful shutdown error", "err", err)
		}
	}()

	slog.Info("signal-server: starting", "addr", addr)

	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("signal-server: fatal listen error", "err", err)
		os.Exit(1)
	}

	slog.Info("signal-server: stopped cleanly")
}
