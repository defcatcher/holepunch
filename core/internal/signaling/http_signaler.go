// Package signaling provides the interface and implementations for exchanging
// WebRTC SDP messages between peers through a central broker. This file
// implements the Signaler interface using HTTP long-polling against the
// HolePunch signaling server.
package signaling

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"github.com/user/holepunch-core/internal/models"
)

// HTTPSignaler implements Signaler by exchanging SDP strings with a remote
// HTTP signaling broker. The broker exposes two endpoints per session:
//
//	POST {baseURL}/signal/{code}/{role}   body: {"sdp":"..."}   → 200 OK
//	GET  {baseURL}/signal/{code}/{role}   resp: {"sdp":"..."}   (long-poll, ≤60 s)
//
// One HTTPSignaler instance is created per TypeConnect event and discarded
// once both the offer and answer have been exchanged. Close is a no-op.
type HTTPSignaler struct {
	baseURL string
	code    string
	client  *http.Client
}

// Compile-time assertion that *HTTPSignaler satisfies the Signaler interface.
var _ Signaler = (*HTTPSignaler)(nil)

// NewHTTPSignaler constructs an HTTPSignaler that talks to the broker at
// baseURL and identifies the negotiation session by code.
//
// The underlying http.Client is configured with a 90-second timeout — enough
// to outlast the server's 60-second long-poll window plus network latency —
// while still providing a hard upper bound against a hung server.
func NewHTTPSignaler(baseURL, code string) *HTTPSignaler {
	return &HTTPSignaler{
		baseURL: baseURL,
		code:    code,
		client: &http.Client{
			Timeout: 90 * time.Second,
		},
	}
}

// PublishSDP POSTs our fully-gathered SDP to the broker under our role so
// that the remote peer can retrieve it via WaitForSDP. The call completes as
// soon as the broker acknowledges the write (HTTP 200).
//
// The ctx deadline governs the entire POST round-trip. If the broker is
// unreachable or returns a non-200 status, a descriptive error is returned.
func (s *HTTPSignaler) PublishSDP(ctx context.Context, role models.Role, sdp string) error {
	body, err := json.Marshal(struct {
		SDP string `json:"sdp"`
	}{SDP: sdp})
	if err != nil {
		return fmt.Errorf("signaling: marshal SDP body: %w", err)
	}

	url := s.endpoint(role)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("signaling: build POST request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	slog.Info("signaling: publishing SDP", "role", role, "url", url)

	resp, err := s.client.Do(req)
	if err != nil {
		return fmt.Errorf("signaling: POST SDP to broker: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("signaling: broker POST returned HTTP %d: %s",
			resp.StatusCode, string(snippet))
	}

	slog.Info("signaling: SDP published successfully", "role", role)
	return nil
}

// WaitForSDP issues a long-polling GET to the broker and blocks until the
// remote peer's SDP for the given role becomes available, or until ctx is
// cancelled / the request times out.
//
// The broker holds the GET connection open for up to 60 seconds. The client's
// 90-second http.Client timeout means we will outlast the server's wait
// window and receive a proper 504 timeout response rather than a client-side
// deadline-exceeded error in the common case.
//
// On success the raw SDP string is returned. An error is returned if the
// broker is unreachable, returns a non-200 status, or sends a malformed body.
func (s *HTTPSignaler) WaitForSDP(ctx context.Context, role models.Role) (string, error) {
	url := s.endpoint(role)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", fmt.Errorf("signaling: build GET request: %w", err)
	}

	slog.Info("signaling: waiting for remote SDP", "role", role, "url", url)

	resp, err := s.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("signaling: GET SDP from broker: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return "", fmt.Errorf("signaling: broker GET returned HTTP %d: %s",
			resp.StatusCode, string(snippet))
	}

	var result struct {
		SDP string `json:"sdp"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("signaling: decode broker response: %w", err)
	}
	if result.SDP == "" {
		return "", fmt.Errorf("signaling: broker returned an empty SDP for role %q", role)
	}

	slog.Info("signaling: received remote SDP", "role", role, "sdp_len", len(result.SDP))
	return result.SDP, nil
}

// Close is a no-op. HTTPSignaler holds no persistent connections; the
// underlying http.Client uses ephemeral request connections that are closed
// after each round-trip. This method exists solely to satisfy the Signaler
// interface.
func (s *HTTPSignaler) Close() error { return nil }

// endpoint builds the broker URL for the given role:
//
//	{baseURL}/signal/{code}/{role}
func (s *HTTPSignaler) endpoint(role models.Role) string {
	return fmt.Sprintf("%s/signal/%s/%s", s.baseURL, s.code, string(role))
}
