// Package ipc implements the local TCP transport between the Python GUI client
// and the Go backend process. This file owns the server lifecycle: accepting
// connections, reading length-prefixed frames from Python, dispatching control
// messages or binary chunks to the p2p.Engine, and writing Engine callbacks
// (status updates, forwarded peer frames) back to Python.
package ipc

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sync"

	"github.com/user/holepunch-core/internal/models"
	"github.com/user/holepunch-core/internal/p2p"
	"github.com/user/holepunch-core/internal/signaling"
)

// Server is a single-connection TCP IPC server. It accepts one Python client
// at a time and re-enters the accept loop after the connection closes, which
// allows Python to reconnect without restarting the Go binary.
type Server struct {
	addr        string   // TCP listen address, e.g. "127.0.0.1:1488"
	signalBase  string   // Signaling broker base URL, e.g. "https://signal.example.com"
	stunServers []string // STUN URIs forwarded to p2p.NewEngine
}

// NewServer constructs a Server. stunServers may be nil or empty; p2p.NewEngine
// will apply its own defaults (stun:stun.l.google.com:19302).
func NewServer(addr, signalBase string, stunServers []string) *Server {
	return &Server{
		addr:        addr,
		signalBase:  signalBase,
		stunServers: stunServers,
	}
}

// ListenAndServe starts the TCP listener and blocks, serving one Python
// connection at a time. It returns nil when ctx is cancelled (clean shutdown)
// or a non-nil error on a fatal listener failure.
func (s *Server) ListenAndServe(ctx context.Context) error {
	lc := net.ListenConfig{}
	ln, err := lc.Listen(ctx, "tcp", s.addr)
	if err != nil {
		return fmt.Errorf("ipc: listen %s: %w", s.addr, err)
	}
	slog.Info("ipc: listening for Python client", "addr", s.addr)

	// Close the listener when the context is cancelled so that Accept unblocks.
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				// The context was cancelled — this is expected during shutdown.
				return nil
			}
			return fmt.Errorf("ipc: accept: %w", err)
		}
		slog.Info("ipc: Python client connected", "remote", conn.RemoteAddr())
		s.handleConn(ctx, conn)
		slog.Info("ipc: Python client disconnected", "remote", conn.RemoteAddr())
	}
}

// ---------------------------------------------------------------------------
// session — per-connection mutable state
// ---------------------------------------------------------------------------

// session holds all mutable state for one live Python IPC connection.
// Fields guarded by wMu (write path) or enMu (engine/cancel) are noted inline.
type session struct {
	conn net.Conn
	srv  *Server

	// wMu serialises all writes to conn. Engine callbacks fire on goroutines
	// other than the read loop, so every write must hold this mutex.
	wMu sync.Mutex

	// enMu guards engine and cancel. Both are set together inside handleConnect
	// and torn down together inside closeEngine.
	enMu   sync.Mutex
	engine *p2p.Engine
	cancel context.CancelFunc
}

// writeJSON marshals v and sends it as a length-prefixed IPC frame.
// Safe to call from any goroutine.
func (s *session) writeJSON(v any) {
	payload, err := json.Marshal(v)
	if err != nil {
		slog.Error("ipc: json.Marshal failed", "err", err)
		return
	}
	s.wMu.Lock()
	defer s.wMu.Unlock()
	if err := WriteMsg(s.conn, payload); err != nil {
		slog.Warn("ipc: write json failed", "err", err)
	}
}

// writeRaw sends raw bytes as a length-prefixed IPC frame without any
// marshalling. Used to forward peer frames (both text and binary) to Python.
// Safe to call from any goroutine.
func (s *session) writeRaw(data []byte) {
	s.wMu.Lock()
	defer s.wMu.Unlock()
	if err := WriteMsg(s.conn, data); err != nil {
		slog.Warn("ipc: write raw failed", "err", err)
	}
}

// closeEngine cancels the in-flight Connect goroutine (if any) and closes the
// Engine. Idempotent: safe to call when engine is nil. Must NOT be called with
// enMu held — it acquires the mutex internally.
func (s *session) closeEngine() {
	s.enMu.Lock()
	eng := s.engine
	cancel := s.cancel
	s.engine = nil
	s.cancel = nil
	s.enMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if eng != nil {
		if err := eng.Close(); err != nil {
			slog.Warn("ipc: engine.Close error", "err", err)
		}
	}
}

// ---------------------------------------------------------------------------
// handleConn — per-connection read loop
// ---------------------------------------------------------------------------

// handleConn runs the blocking read loop for one Python connection. It returns
// when the connection is closed (by Python, by the OS, or by a ctx cancellation).
func (s *Server) handleConn(ctx context.Context, conn net.Conn) {
	sess := &session{
		conn: conn,
		srv:  s,
	}

	// connDone is closed when handleConn returns. The goroutine below uses it
	// to avoid leaking after the connection has already been cleaned up.
	connDone := make(chan struct{})

	defer func() {
		close(connDone)
		sess.closeEngine()
		_ = conn.Close()
	}()

	// When the parent context is cancelled (SIGTERM / shutdown), force-close
	// the connection so that the blocking ReadMsg call returns immediately.
	go func() {
		select {
		case <-ctx.Done():
			_ = conn.Close()
		case <-connDone:
		}
	}()

	for {
		payload, err := ReadMsg(conn)
		if err != nil {
			if err == io.EOF || err == io.ErrUnexpectedEOF || ctx.Err() != nil {
				return // clean disconnect or shutdown
			}
			slog.Warn("ipc: read error", "err", err)
			return
		}
		sess.dispatch(ctx, payload)
	}
}

// ---------------------------------------------------------------------------
// dispatch — routes one incoming frame from Python
// ---------------------------------------------------------------------------

// dispatch classifies payload as a JSON control message or a binary encrypted
// chunk, then routes it to the appropriate handler.
func (sess *session) dispatch(ctx context.Context, payload []byte) {
	// Fast-path: attempt to unmarshal as a JSON control message. Encrypted
	// chunks are AES-GCM ciphertext and will virtually never parse as valid
	// JSON, so the heuristic is reliable in practice (and matches ipc_link.py).
	var base models.BaseMessage
	if err := json.Unmarshal(payload, &base); err == nil {
		sess.dispatchControl(ctx, base.Type, payload)
		return
	}

	// Slow-path: binary encrypted chunk — forward to the remote peer as a
	// DataChannel binary frame.
	sess.enMu.Lock()
	eng := sess.engine
	sess.enMu.Unlock()

	if eng == nil {
		slog.Warn("ipc: binary chunk received before TypeConnect — dropping",
			"bytes", len(payload))
		return
	}
	if err := eng.Send(payload); err != nil {
		slog.Warn("ipc: Engine.Send failed", "err", err)
	}
}

// dispatchControl routes a parsed JSON control message to the correct handler.
func (sess *session) dispatchControl(ctx context.Context, typ models.MessageType, raw []byte) {
	switch typ {
	case models.TypeConnect:
		var msg models.ConnectMsg
		if err := json.Unmarshal(raw, &msg); err != nil {
			slog.Warn("ipc: malformed TypeConnect", "err", err)
			sess.writeJSON(models.NewError("malformed connect message"))
			return
		}
		sess.handleConnect(ctx, msg)

	case models.TypeMetadata, models.TypeReady, models.TypeError:
		// Forward the raw JSON bytes verbatim to the remote peer as a
		// DataChannel text frame. The receiver's IPC server will write them
		// straight to its Python client — no re-encoding needed.
		sess.enMu.Lock()
		eng := sess.engine
		sess.enMu.Unlock()

		if eng == nil {
			slog.Warn("ipc: control message before TypeConnect — dropping",
				"type", typ)
			return
		}
		if err := eng.SendText(string(raw)); err != nil {
			slog.Warn("ipc: Engine.SendText failed", "err", err, "type", typ)
		}

	default:
		slog.Warn("ipc: unknown message type — ignoring", "type", typ)
	}
}

// ---------------------------------------------------------------------------
// handleConnect — creates a new Engine and starts P2P negotiation
// ---------------------------------------------------------------------------

// handleConnect tears down any previous Engine, creates a fresh one for the
// requested role, wires its callbacks to write back to Python, and launches
// WebRTC negotiation via the signaling broker in a background goroutine.
func (sess *session) handleConnect(ctx context.Context, msg models.ConnectMsg) {
	slog.Info("ipc: TypeConnect received", "code", msg.Code, "role", msg.Role)

	// Tear down any previous engine before creating a new one. This handles
	// the case where Python sends a second TypeConnect (e.g. re-connect).
	sess.closeEngine()

	eng, err := p2p.NewEngine(p2p.Config{
		Role:        msg.Role,
		STUNServers: sess.srv.stunServers,
	})
	if err != nil {
		slog.Error("ipc: p2p.NewEngine failed", "err", err)
		sess.writeJSON(models.NewError(fmt.Sprintf("engine init failed: %s", err)))
		return
	}

	// Create a child context so we can cancel the Connect goroutine
	// independently of the parent (e.g. when a new TypeConnect arrives).
	connCtx, cancel := context.WithCancel(ctx)

	sess.enMu.Lock()
	sess.engine = eng
	sess.cancel = cancel
	sess.enMu.Unlock()

	// ── Engine → Python callbacks ────────────────────────────────────────────

	eng.OnConnected = func() {
		slog.Info("ipc: DataChannel open — sending status:connected to Python")
		sess.writeJSON(models.NewStatus(models.StatusConnected))
	}

	eng.OnDisconnected = func() {
		slog.Info("ipc: P2P disconnected — sending status:disconnected to Python")
		sess.writeJSON(models.NewStatus(models.StatusDisconnected))
	}

	// Every DataChannel frame (text or binary) is forwarded as-is to Python.
	// Text frames carry JSON (metadata / ready / error envelopes); binary
	// frames carry raw AES-GCM encrypted chunks. Python's ipc_link.py
	// distinguishes the two with its try/except JSON heuristic.
	eng.OnMessage = func(data []byte, _ bool) {
		sess.writeRaw(data)
	}

	// ── Notify Python that negotiation is starting ───────────────────────────
	sess.writeJSON(models.NewStatus(models.StatusConnecting))

	// ── Launch signaling + ICE in the background ─────────────────────────────
	// engine.Connect() is a blocking call: it exchanges SDP with the broker
	// and waits for ICE gathering to complete before returning. We must not
	// block the IPC read loop while this is in progress.
	sig := signaling.NewHTTPSignaler(sess.srv.signalBase, msg.Code)
	go func() {
		defer sig.Close()
		if err := eng.Connect(connCtx, sig); err != nil {
			if connCtx.Err() != nil {
				// Context was cancelled by closeEngine (new TypeConnect or
				// shutdown) — this is not an error worth reporting to Python.
				return
			}
			slog.Error("ipc: engine.Connect failed", "err", err)
			sess.writeJSON(models.NewError(
				fmt.Sprintf("P2P negotiation failed: %s", err),
			))
		}
	}()
}
