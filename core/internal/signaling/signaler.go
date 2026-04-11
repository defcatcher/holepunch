// Package signaling provides the interface for exchanging WebRTC SDP offers
// and answers between peers, typically through a relay server or broker.
package signaling

import (
	"context"

	"github.com/user/holepunch-core/internal/models"
)

// Signaler is responsible for exchanging WebRTC SDP (Session Description Protocol)
// messages between two peers. The implementation may use a centralized signaling
// server, a peer code lookup service, or any other mechanism to route offers
// and answers between the two sides.
type Signaler interface {
	// PublishSDP sends the SDP for the given role to the remote peer.
	// ctx can be used to cancel the operation or enforce a deadline.
	PublishSDP(ctx context.Context, role models.Role, sdp string) error

	// WaitForSDP blocks until an SDP from the remote peer with the given role
	// is available, then returns it. ctx can be used to cancel the wait.
	WaitForSDP(ctx context.Context, role models.Role) (string, error)

	// Close closes the signaler and releases all resources.
	Close() error
}
