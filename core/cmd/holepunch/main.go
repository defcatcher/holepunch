// Command holepunch is the Go backend process for the HolePunch P2P file
// transfer application. It is spawned automatically by the Python GUI (main.py)
// at startup and terminated via SIGTERM when the GUI window closes.
//
// Responsibilities:
//   - Listen on a local TCP port for the Python IPC client.
//   - On TypeConnect: negotiate a WebRTC DataChannel with the remote peer
//     through the configured signaling broker.
//   - Relay control messages (metadata, ready, error) and raw encrypted chunks
//     bidirectionally between the Python client and the WebRTC DataChannel.
//   - Exit cleanly on SIGTERM or SIGINT so no zombie processes are left behind.
//
// Usage:
//
//	holepunch [flags]
//
//	-ipc-addr   string   TCP address for the Python IPC socket (default "127.0.0.1:1488")
//	-signal-url string   Signaling broker base URL            (default "http://localhost:8080")
//	-stun       string   STUN server URI                      (default "stun:stun.l.google.com:19302")
package main

import (
	"context"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/user/holepunch-core/internal/ipc"
)

func main() {
	// ── CLI flags ────────────────────────────────────────────────────────────
	ipcAddr := flag.String(
		"ipc-addr",
		"127.0.0.1:1488",
		"TCP listen address for the Python IPC socket",
	)
	signalURL := flag.String(
		"signal-url",
		"http://localhost:8080",
		"Base URL of the HolePunch signaling broker",
	)
	stunFlag := flag.String(
		"stun",
		"stun:stun.l.google.com:19302",
		"Comma-separated list of STUN server URIs",
	)
	flag.Parse()

	// Support comma-separated STUN servers in a single flag value, e.g.:
	//   --stun "stun:stun1.l.google.com:19302,stun:stun2.l.google.com:19302"
	stunServers := splitAndTrim(*stunFlag)

	// ── Structured logging ───────────────────────────────────────────────────
	// slog defaults to INFO level on os.Stderr, which Python reads from the
	// subprocess's stderr pipe for debugging.
	slog.Info("holepunch: starting",
		"ipc_addr", *ipcAddr,
		"signal_url", *signalURL,
		"stun_servers", stunServers,
	)

	// ── Root context — cancelled on SIGTERM / SIGINT ─────────────────────────
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Intercept OS signals in a dedicated goroutine. When the Python GUI exits
	// it calls atexit which sends SIGTERM to this process. SIGINT handles
	// Ctrl-C during manual testing.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	go func() {
		sig := <-sigCh
		slog.Info("holepunch: received signal — shutting down", "signal", sig.String())
		// Cancelling the root context unblocks ListenAndServe, closes the TCP
		// listener, cancels any in-flight engine.Connect call, and closes the
		// active WebRTC PeerConnection via the deferred cleanup in handleConn.
		cancel()
	}()

	// ── IPC server ───────────────────────────────────────────────────────────
	srv := ipc.NewServer(*ipcAddr, *signalURL, stunServers)

	if err := srv.ListenAndServe(ctx); err != nil {
		slog.Error("holepunch: IPC server error", "err", err)
		os.Exit(1)
	}

	slog.Info("holepunch: goodbye")
}

// splitAndTrim splits s on commas and trims whitespace from each token,
// returning only non-empty entries. It is used to parse the --stun flag.
func splitAndTrim(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if trimmed := strings.TrimSpace(p); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}
