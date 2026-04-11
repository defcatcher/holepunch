# INTERFACE.md — IPC Protocol Specification

This document defines the communication protocol between the Python client and the Go backend over a local TCP socket (default port: **1488**).

---

## Transport Layer

- **Protocol:** TCP, `localhost` only
- **Framing:** Every message (JSON or binary) is prefixed with a **4-byte big-endian unsigned integer** indicating the payload length.

```
┌──────────────────────┬──────────────────────────────────┐
│  Length (4 bytes BE) │  Payload (N bytes)               │
└──────────────────────┴──────────────────────────────────┘
```

Python uses `struct.pack('>I', len(payload))` to write the header.  
Go should use `binary.BigEndian.Uint32` to read it.

---

## Message Types

### 1. `metadata` — Sender → Go → Receiver

Sent by the **Python sender** before the file transfer begins.

```json
{
  "type": "metadata",
  "name": "archive.zip",
  "size": 104857600
}
```

| Field  | Type   | Description                  |
|--------|--------|------------------------------|
| `type` | string | Always `"metadata"`          |
| `name` | string | Original filename            |
| `size` | int    | File size in bytes (plaintext) |

Go must forward this message to the receiver's Python client as-is.

---

### 2. `ready` — Receiver → Go → Sender

Sent by the **Python receiver** after the user accepts the incoming file and the decryptor is initialized.

```json
{
  "type": "ready"
}
```

Go must forward this to the sender's Python client. Upon receiving `ready`, the sender starts streaming encrypted chunks.

---

### 3. `error` — Either side → Go → Other side

Sent when something goes wrong. Go must forward to the other peer.

```json
{
  "type": "error",
  "msg": "Wrong Password or PIN mismatch"
}
```

| Field  | Type   | Description       |
|--------|--------|-------------------|
| `type` | string | Always `"error"`  |
| `msg`  | string | Human-readable reason |

---

### 4. Encrypted Chunks — Sender → Go → Receiver

Binary payload. Not JSON. Go must forward raw bytes to the receiver.

#### Chunk structure (Python produces this):

```
┌─────────────┬──────────────────────────────────────────────────────┐
│ Nonce       │ Ciphertext                                           │
│ (12 bytes)  │ (original chunk bytes + 16-byte GCM auth tag)        │
└─────────────┴──────────────────────────────────────────────────────┘
```

- Total overhead per chunk: **28 bytes** (12 nonce + 16 GCM tag)
- Max plaintext per chunk: **65 536 bytes** (64 KB)
- Max total chunk size: **65 564 bytes**
- Each chunk uses a **fresh random nonce** — never reused

Go does **not** need to decrypt these. Just forward the bytes as a framed binary message.

---

## How Go distinguishes JSON from binary chunks

**Go should NOT use heuristics for this.** The recommended approach is to add a **1-byte type prefix** before the 4-byte length header:

```
┌──────────────┬──────────────────────┬──────────────┐
│ Type (1 byte)│  Length (4 bytes BE) │  Payload     │
└──────────────┴──────────────────────┴──────────────┘

0x01 = JSON message
0x02 = Binary chunk
```

> ⚠️ The current Python client uses try/except heuristics (try UTF-8 decode → try JSON parse → else treat as binary). This works in practice but is fragile. If the Go backend uses a type-prefix framing, update src/ipc_link.py accordingly.
---

## Full Transfer Flow

```
Python Sender          Go Backend              Python Receiver
     │                     │                        │
     │──── metadata ───────►│──── metadata ──────────►│
     │                     │                        │  (user accepts, decryptor init)
     │                     │◄─── ready ─────────────│
     │◄─── ready ──────────│                        │
     │                     │                        │
     │──── chunk[0] ───────►│──── chunk[0] ──────────►│
     │──── chunk[1] ───────►│──── chunk[1] ──────────►│
     │        ...          │          ...           │
     │──── chunk[N] ───────►│──── chunk[N] ──────────►│
     │                     │                        │  (decryptor closes file)
```

If at any point decryption fails on the receiver side:
```
     │◄─── error ──────────│◄── error ──────────────│
```
The sender stops the encryption thread. The partial file is deleted on the receiver side.

---

## Peer Code

The 6-digit peer code (`XXX-XXX`) generated by the sender is used as the **PBKDF2 salt** during key derivation. It is never transmitted over the network by Python — Go's signaling layer is responsible for exchanging peer addresses. The Python app just needs the user to manually share/enter this code out-of-band.
