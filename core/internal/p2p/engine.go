// Package p2p implements the WebRTC-based peer-to-peer transport layer for
// HolePunch. This file owns the PeerConnection lifecycle: ICE configuration,
// SDP offer/answer negotiation through a Signaler, and DataChannel setup.
package p2p

import (
	"context"
	"fmt"
	"log/slog"
	"sync"

	"github.com/pion/webrtc/v3"
	"github.com/user/holepunch-core/internal/models"
	"github.com/user/holepunch-core/internal/signaling"
)

// defaultSTUNServers is used when Config.STUNServers is left empty.
var defaultSTUNServers = []string{"stun:stun.l.google.com:19302"}

// defaultDataChannelLabel is used when Config.DataChannelLabel is left empty.
const defaultDataChannelLabel = "holepunch"

// Config holds the tunable parameters for the WebRTC engine.
type Config struct {
	// STUNServers is the list of STUN server URIs used during ICE gathering.
	// Defaults to ["stun:stun.l.google.com:19302"] when empty.
	STUNServers []string

	// Role determines whether this peer acts as the WebRTC offerer (sender)
	// or answerer (receiver) during negotiation.
	Role models.Role

	// DataChannelLabel is the negotiated label for the WebRTC DataChannel.
	// Defaults to "holepunch" when empty.
	DataChannelLabel string
}

// Engine orchestrates the WebRTC PeerConnection lifecycle for HolePunch.
// It drives ICE negotiation through a Signaler, manages a DataChannel, and
// exposes a thin callback-based API to the rest of the application.
type Engine struct {
	cfg config

	pc *webrtc.PeerConnection

	mu        sync.Mutex
	transport *DataTransport

	// OnConnected is called once the DataChannel transitions to the open state.
	// Guaranteed to be invoked at most once per Engine.
	OnConnected func()

	// OnMessage is called for every frame received over the DataChannel.
	// isText mirrors the WebRTC DataChannel text/binary distinction.
	OnMessage func(data []byte, isText bool)

	// OnDisconnected is called when the PeerConnection reaches a terminal
	// disconnected, failed, or closed state.
	OnDisconnected func()
}

// config is an internal copy of Config with all defaults applied.
type config struct {
	stunServers      []string
	role             models.Role
	dataChannelLabel string
}

// applyDefaults returns a config with all zero-value fields replaced by their
// documented defaults.
func applyDefaults(cfg Config) config {
	c := config{
		stunServers:      cfg.STUNServers,
		role:             cfg.Role,
		dataChannelLabel: cfg.DataChannelLabel,
	}
	if len(c.stunServers) == 0 {
		c.stunServers = defaultSTUNServers
	}
	if c.dataChannelLabel == "" {
		c.dataChannelLabel = defaultDataChannelLabel
	}
	return c
}

// NewEngine creates a pion PeerConnection configured with the ICE STUN servers
// from cfg and returns a ready-to-use Engine. Connect must be called next to
// perform SDP negotiation.
func NewEngine(cfg Config) (*Engine, error) {
	c := applyDefaults(cfg)

	iceServers := make([]webrtc.ICEServer, len(c.stunServers))
	for i, uri := range c.stunServers {
		iceServers[i] = webrtc.ICEServer{URLs: []string{uri}}
	}

	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{
		ICEServers: iceServers,
	})
	if err != nil {
		return nil, fmt.Errorf("p2p: create PeerConnection: %w", err)
	}

	e := &Engine{
		cfg: c,
		pc:  pc,
		// No-op defaults so callers never have to nil-check the callbacks.
		OnConnected:    func() {},
		OnMessage:      func([]byte, bool) {},
		OnDisconnected: func() {},
	}

	// Monitor connection state so we can surface disconnection events upward.
	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		slog.Info("p2p: connection state changed", "state", state.String())
		switch state {
		case webrtc.PeerConnectionStateDisconnected,
			webrtc.PeerConnectionStateFailed,
			webrtc.PeerConnectionStateClosed:
			e.OnDisconnected()
		}
	})

	return e, nil
}

// Connect drives the full WebRTC negotiation via signaler, dispatching to
// connectAsSender or connectAsReceiver based on the configured Role.
// It returns once negotiation is complete; the DataChannel may still be
// opening at that point — OnConnected fires asynchronously when it does.
func (e *Engine) Connect(ctx context.Context, signaler signaling.Signaler) error {
	switch e.cfg.role {
	case models.RoleSender:
		return e.connectAsSender(ctx, signaler)
	case models.RoleReceiver:
		return e.connectAsReceiver(ctx, signaler)
	default:
		return fmt.Errorf("p2p: unknown role %q", e.cfg.role)
	}
}

// connectAsSender performs the WebRTC offerer path:
//  1. Creates a reliable ordered DataChannel.
//  2. Creates an SDP offer and sets it as the local description.
//  3. Waits for ICE gathering to complete.
//  4. Publishes the trickle-complete offer SDP through the signaler.
//  5. Waits for the answerer's SDP and sets it as the remote description.
func (e *Engine) connectAsSender(ctx context.Context, signaler signaling.Signaler) error {
	slog.Info("p2p: starting as sender (offerer)",
		"label", e.cfg.dataChannelLabel)

	ordered := true
	dc, err := e.pc.CreateDataChannel(e.cfg.dataChannelLabel, &webrtc.DataChannelInit{
		Ordered: &ordered,
	})
	if err != nil {
		return fmt.Errorf("p2p: CreateDataChannel: %w", err)
	}

	e.wireDataChannel(dc)

	offer, err := e.pc.CreateOffer(nil)
	if err != nil {
		return fmt.Errorf("p2p: CreateOffer: %w", err)
	}

	// GatheringCompletePromise must be obtained before SetLocalDescription so
	// that the promise is registered before ICE gathering begins and we cannot
	// miss the completion event.
	gatherDone := webrtc.GatheringCompletePromise(e.pc)

	if err := e.pc.SetLocalDescription(offer); err != nil {
		return fmt.Errorf("p2p: SetLocalDescription (offer): %w", err)
	}

	slog.Info("p2p: waiting for ICE gathering to complete")
	select {
	case <-gatherDone:
	case <-ctx.Done():
		return fmt.Errorf("p2p: context cancelled while gathering ICE candidates: %w", ctx.Err())
	}

	finalSDP := e.pc.LocalDescription().SDP
	slog.Info("p2p: publishing offer SDP to signaler")

	if err := signaler.PublishSDP(ctx, models.RoleSender, finalSDP); err != nil {
		return fmt.Errorf("p2p: PublishSDP (offer): %w", err)
	}

	slog.Info("p2p: waiting for answer SDP from receiver")
	answerSDP, err := signaler.WaitForSDP(ctx, models.RoleReceiver)
	if err != nil {
		return fmt.Errorf("p2p: WaitForSDP (answer): %w", err)
	}

	if err := e.pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  answerSDP,
	}); err != nil {
		return fmt.Errorf("p2p: SetRemoteDescription (answer): %w", err)
	}

	slog.Info("p2p: sender negotiation complete")
	return nil
}

// connectAsReceiver performs the WebRTC answerer path:
//  1. Registers OnDataChannel to capture the incoming DataChannel.
//  2. Waits for the offerer's SDP and sets it as the remote description.
//  3. Creates an SDP answer and sets it as the local description.
//  4. Waits for ICE gathering to complete.
//  5. Publishes the trickle-complete answer SDP through the signaler.
func (e *Engine) connectAsReceiver(ctx context.Context, signaler signaling.Signaler) error {
	slog.Info("p2p: starting as receiver (answerer)")

	// Register OnDataChannel before setting the remote description so we
	// can never miss the DataChannel event that pion emits during negotiation.
	e.pc.OnDataChannel(func(dc *webrtc.DataChannel) {
		slog.Info("p2p: DataChannel offered by remote peer", "label", dc.Label())
		e.wireDataChannel(dc)
	})

	slog.Info("p2p: waiting for offer SDP from sender")
	offerSDP, err := signaler.WaitForSDP(ctx, models.RoleSender)
	if err != nil {
		return fmt.Errorf("p2p: WaitForSDP (offer): %w", err)
	}

	if err := e.pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  offerSDP,
	}); err != nil {
		return fmt.Errorf("p2p: SetRemoteDescription (offer): %w", err)
	}

	answer, err := e.pc.CreateAnswer(nil)
	if err != nil {
		return fmt.Errorf("p2p: CreateAnswer: %w", err)
	}

	// Again, register the promise before SetLocalDescription triggers gathering.
	gatherDone := webrtc.GatheringCompletePromise(e.pc)

	if err := e.pc.SetLocalDescription(answer); err != nil {
		return fmt.Errorf("p2p: SetLocalDescription (answer): %w", err)
	}

	slog.Info("p2p: waiting for ICE gathering to complete")
	select {
	case <-gatherDone:
	case <-ctx.Done():
		return fmt.Errorf("p2p: context cancelled while gathering ICE candidates: %w", ctx.Err())
	}

	finalSDP := e.pc.LocalDescription().SDP
	slog.Info("p2p: publishing answer SDP to signaler")

	if err := signaler.PublishSDP(ctx, models.RoleReceiver, finalSDP); err != nil {
		return fmt.Errorf("p2p: PublishSDP (answer): %w", err)
	}

	slog.Info("p2p: receiver negotiation complete")
	return nil
}

// wireDataChannel attaches an OnOpen handler to dc that creates the
// DataTransport, fires OnConnected, and starts the inbound message pump.
// It is shared by both connectAsSender and connectAsReceiver to keep the
// DataChannel lifecycle logic in one place.
func (e *Engine) wireDataChannel(dc *webrtc.DataChannel) {
	dc.OnOpen(func() {
		slog.Info("p2p: DataChannel opened",
			"label", dc.Label(),
			"id", dc.ID())

		t := NewDataTransport(dc)

		e.mu.Lock()
		e.transport = t
		e.mu.Unlock()

		e.OnConnected()

		// Pump inbound frames to the OnMessage callback on a dedicated
		// goroutine so we never block pion's internal SCTP read loop.
		go e.pumpMessages(t)
	})
}

// pumpMessages drains t until the transport closes, forwarding every received
// frame to the OnMessage callback. It exits cleanly on io.EOF (normal close)
// or any other error returned by Recv.
func (e *Engine) pumpMessages(t *DataTransport) {
	// Use a background context: the pump should run for the full lifetime of
	// the transport, not tied to any single caller's context.
	ctx := context.Background()
	for {
		msg, err := t.Recv(ctx)
		if err != nil {
			slog.Info("p2p: inbound message pump stopped", "reason", err.Error())
			return
		}
		e.OnMessage(msg.Data, msg.IsText)
	}
}

// SendText transmits text as a UTF-8 DataChannel frame.
// Returns an error if the DataChannel is not yet open.
func (e *Engine) SendText(text string) error {
	t := e.getTransport()
	if t == nil {
		return fmt.Errorf("p2p: DataChannel is not open")
	}
	return t.SendText(text)
}

// Send transmits data as a binary DataChannel frame.
// Returns an error if the DataChannel is not yet open.
func (e *Engine) Send(data []byte) error {
	t := e.getTransport()
	if t == nil {
		return fmt.Errorf("p2p: DataChannel is not open")
	}
	return t.Send(data)
}

// Close tears down the DataTransport (if open) and then closes the underlying
// PeerConnection. It is safe to call more than once.
func (e *Engine) Close() error {
	slog.Info("p2p: closing engine")

	e.mu.Lock()
	t := e.transport
	e.mu.Unlock()

	if t != nil {
		t.Close()
	}

	return e.pc.Close()
}

// getTransport returns the current DataTransport under the mutex.
func (e *Engine) getTransport() *DataTransport {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.transport
}
