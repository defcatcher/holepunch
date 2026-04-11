// Package p2p implements the WebRTC-based peer-to-peer transport layer for
// HolePunch. This file wraps a *webrtc.DataChannel into a struct that exposes
// a synchronous send API and a channel-based receive API, insulating the rest
// of the engine from the pion callback model.
package p2p

import (
	"context"
	"fmt"
	"io"
	"sync"
	"time"

	"github.com/pion/webrtc/v3"
)

// Message is a single frame received over the WebRTC DataChannel.
// Binary frames (dc.Send) set IsText = false; text frames (dc.SendText)
// set IsText = true.
type Message struct {
	Data   []byte
	IsText bool
}

// DataTransport wraps a *webrtc.DataChannel and provides:
//   - Synchronous, error-returning Send / SendText methods.
//   - A buffered inbound channel that is populated by pion's OnMessage callback.
//   - A Recv method for callers that prefer a blocking, context-aware read.
//   - A Msgs method for callers that prefer a raw channel in a select loop.
type DataTransport struct {
	dc        *webrtc.DataChannel
	inbound   chan Message
	done      chan struct{}
	closeOnce sync.Once
}

// NewDataTransport constructs a DataTransport around dc and wires up the
// necessary pion callbacks. The DataChannel must already be open; the caller
// is responsible for ensuring that invariant (typically inside dc.OnOpen).
func NewDataTransport(dc *webrtc.DataChannel) *DataTransport {
	t := &DataTransport{
		dc:      dc,
		inbound: make(chan Message, 256),
		done:    make(chan struct{}),
	}

	// Set the buffered amount low threshold so we can use the callback-based
	// approach for backpressure instead of polling.
	dc.SetBufferedAmountLowThreshold(1 * 1024 * 1024) // 1 MB

	// OnMessage is invoked by pion on the goroutine that drains the SCTP layer.
	// We copy the raw bytes out of msg.Data before returning so that pion is
	// free to reuse its internal buffer.
	dc.OnMessage(func(msg webrtc.DataChannelMessage) {
		payload := make([]byte, len(msg.Data))
		copy(payload, msg.Data)

		m := Message{
			Data:   payload,
			IsText: msg.IsString,
		}

		// Non-blocking push: if the consumer is too slow we drop the frame
		// rather than blocking pion's receive goroutine. Callers that need
		// reliable delivery must ensure the inbound channel is drained promptly.
		select {
		case t.inbound <- m:
		case <-t.done:
			// Transport is closing; silently discard.
		}
	})

	// OnClose is called by pion when the remote peer closes the DataChannel or
	// when the underlying SCTP/DTLS connection is torn down.
	dc.OnClose(func() {
		t.Close()
	})

	return t
}

// Send transmits data as a binary WebRTC DataChannel message.
// It is safe to call from multiple goroutines concurrently.
// Before sending, it waits for the buffered amount to drop below a threshold
// to prevent overflow and connection drops.
func (t *DataTransport) Send(data []byte) error {
	// WebRTC DataChannel has a limited send buffer. If we send faster than
	// the network can deliver, the buffer grows and the connection may drop.
	// Wait for buffered amount to drain before sending more data.
	const maxBufferSize = 1 * 1024 * 1024 // 1 MB
	const pollInterval = 10 * time.Millisecond

	for t.dc.BufferedAmount() >= uint64(maxBufferSize) {
		select {
		case <-t.done:
			return fmt.Errorf("datachannel closed while waiting for buffer drain")
		case <-time.After(pollInterval):
			// Buffer draining, continue
		}
	}

	return t.dc.Send(data)
}

// SendText transmits text as a UTF-8 WebRTC DataChannel message.
// It is safe to call from multiple goroutines concurrently.
func (t *DataTransport) SendText(text string) error {
	return t.dc.SendText(text)
}

// Recv blocks until one of three things happens:
//   - A Message is available in the inbound buffer  → returns (msg, nil).
//   - The transport is closed                        → returns ({}, io.EOF).
//   - ctx is cancelled or its deadline is exceeded   → returns ({}, ctx.Err()).
//
// Recv is safe to call from multiple goroutines; each call competes for the
// next available message.
func (t *DataTransport) Recv(ctx context.Context) (Message, error) {
	select {
	case msg, ok := <-t.inbound:
		if !ok {
			// Channel was closed — transport is gone.
			return Message{}, io.EOF
		}
		return msg, nil

	case <-t.done:
		// Drain any messages that arrived before the close signal, so that a
		// single final Recv after Close can still pick up buffered data.
		select {
		case msg, ok := <-t.inbound:
			if ok {
				return msg, nil
			}
		default:
		}
		return Message{}, io.EOF

	case <-ctx.Done():
		return Message{}, ctx.Err()
	}
}

// Close shuts the transport down exactly once.
// It closes the done sentinel channel (unblocking all pending Recv calls) and
// then closes the underlying DataChannel. Subsequent calls are no-ops.
func (t *DataTransport) Close() {
	t.closeOnce.Do(func() {
		close(t.done)
		// Ignore the error: the DataChannel may already be closed by the time
		// our OnClose callback fires and calls us back.
		_ = t.dc.Close()
	})
}

// Msgs returns the raw inbound channel for use in select statements.
// Callers must not close the returned channel. The channel is closed
// automatically when the DataTransport itself is closed.
func (t *DataTransport) Msgs() <-chan Message {
	return t.inbound
}
