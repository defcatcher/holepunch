// Package ipc implements the local TCP transport between the Python GUI client
// and the Go backend process. This file contains the low-level frame codec.
//
// Wire format (matches Python's struct.pack('>I', len(payload))):
//
//	┌──────────────────────┬──────────────────────────────────┐
//	│  Length (4 bytes BE) │  Payload (N bytes)               │
//	└──────────────────────┴──────────────────────────────────┘
//
// The payload is either a UTF-8 JSON object (control message) or raw binary
// bytes (encrypted chunk). Callers distinguish the two by attempting
// json.Unmarshal; this mirrors the heuristic already used in ipc_link.py.
package ipc

import (
	"encoding/binary"
	"fmt"
	"io"
)

// maxPayloadBytes is a hard cap on both incoming and outgoing frame sizes.
// Encrypted chunks are at most ~65 564 bytes (ChunkMaxEncryptedSize); the cap
// is set to 100 MiB to accommodate large control messages while protecting
// against a malformed or malicious length header.
const maxPayloadBytes = 100 * 1024 * 1024 // 100 MiB

// WriteMsg encodes payload as a single length-prefixed IPC frame and writes
// it atomically to w. The 4-byte big-endian header and the payload are
// assembled into one buffer before the write so that a single Write call
// reaches the kernel — preventing a short first write from being observed as
// a partial frame by the reader.
//
// WriteMsg is safe to call concurrently only if w is safe for concurrent
// writes. Callers that share a net.Conn across goroutines must serialise
// calls with a mutex.
func WriteMsg(w io.Writer, payload []byte) error {
	if len(payload) > maxPayloadBytes {
		return fmt.Errorf("ipc: payload too large: %d bytes (max %d)",
			len(payload), maxPayloadBytes)
	}

	// Single allocation: 4-byte header + payload body.
	frame := make([]byte, 4+len(payload))
	binary.BigEndian.PutUint32(frame[:4], uint32(len(payload)))
	copy(frame[4:], payload)

	_, err := w.Write(frame)
	return err
}

// ReadMsg reads exactly one length-prefixed IPC frame from r and returns the
// payload bytes. It uses io.ReadFull for both the header and the body, so it
// will block until the complete frame has arrived or an error occurs.
//
// Return values:
//   - (payload, nil)              — a complete frame was read successfully.
//   - (nil, io.EOF)               — r was closed cleanly before any bytes arrived.
//   - (nil, io.ErrUnexpectedEOF)  — r closed in the middle of a frame.
//   - (nil, other error)          — network or framing error.
func ReadMsg(r io.Reader) ([]byte, error) {
	// Read the 4-byte length header.
	var header [4]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		// io.ReadFull converts a zero-byte EOF into io.EOF, and a partial
		// read EOF into io.ErrUnexpectedEOF — both are meaningful to callers.
		return nil, err
	}

	size := binary.BigEndian.Uint32(header[:])

	// A zero-length frame is valid (e.g. a keep-alive ping); return early.
	if size == 0 {
		return []byte{}, nil
	}

	if uint64(size) > uint64(maxPayloadBytes) {
		return nil, fmt.Errorf("ipc: incoming frame too large: %d bytes (max %d)",
			size, maxPayloadBytes)
	}

	buf := make([]byte, size)
	if _, err := io.ReadFull(r, buf); err != nil {
		return nil, err
	}
	return buf, nil
}
