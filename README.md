<div align="center">

```
██╗  ██╗ ██████╗ ██╗     ███████╗██████╗ ██╗   ██╗███╗   ██╗ ██████╗██╗  ██╗
██║  ██║██╔═══██╗██║     ██╔════╝██╔══██╗██║   ██║████╗  ██║██╔════╝██║  ██║
███████║██║   ██║██║     █████╗  ██████╔╝██║   ██║██╔██╗ ██║██║     ███████║
██╔══██║██║   ██║██║     ██╔══╝  ██╔═══╝ ██║   ██║██║╚██╗██║██║     ██╔══██║
██║  ██║╚██████╔╝███████╗███████╗██║     ╚██████╔╝██║ ╚████║╚██████╗██║  ██║
╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚══════╝╚═╝      ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝
```

**Serverless peer-to-peer encrypted file transfer.**  
No cloud storage. No file size limits. No middleman ever sees your data.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![Go](https://img.shields.io/badge/Go-1.22+-00ADD8?style=flat-square&logo=go&logoColor=white)
![PyQt6](https://img.shields.io/badge/PyQt6-GUI-41CD52?style=flat-square)
![WebRTC](https://img.shields.io/badge/WebRTC-pion%2Fv3-orange?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

</div>

---

## What Is HolePunch?

HolePunch lets two people transfer a file **directly between their machines** — through NAT, firewalls, and carrier-grade NAT — without uploading anything to a server. The file travels encrypted, peer-to-peer, straight from one machine to the other.

The architecture is a deliberate split:

- A **Python/PyQt6 GUI** handles the user experience and performs **AES-256-GCM encryption** on the sender's machine before any data leaves.
- A **Go binary** (`holepunch`) handles all real networking: WebRTC ICE hole-punching, DTLS transport, and SCTP DataChannel framing via [pion/webrtc](https://github.com/pion/webrtc). It talks to Python over a local TCP socket.
- A tiny **HTTP signaling broker** (`signal-server`) — deployable for free on Google Cloud Run — helps the two Go peers exchange SDP metadata. Once the direct path is established the broker is no longer involved.

The Go backend is spawned automatically when `main.py` starts. There is nothing to configure for a first run beyond building the binary once.

---

## ✨ Features

| Feature | Detail |
|---|---|
| 🔒 End-to-end encryption | AES-256-GCM per chunk; Go never touches plaintext |
| 🚀 Unlimited file size | Streaming 64 KiB chunks, never fully buffered in memory |
| 🌐 NAT traversal | WebRTC ICE handles symmetric NAT, CGNAT, and most firewalls |
| 🖥️ Cross-platform | Windows, macOS (Intel + Apple Silicon), Linux |
| 🔌 Single-button launch | `python main.py` auto-starts Go; no manual backend step |
| 🎨 Two themes | Dark (default) and Light — switchable at runtime |
| 🪶 Zero server storage | Signal broker is stateless; it only relays ~4 KB of SDP text |
| Automatic Cleanup | The receiver is prompted to delete incomplete or corrupted files if a transfer is interrupted or cancelled |
| Sender Session Reset | If the signaling window (60s) expires, the sender can generate a new peer code and restart the engine in one click |
| Extension Guard | Protects against accidental file corruption by ensuring the correct file extension (e.g., .tar) is preserved during the "Save As" dialog |

---

## 🏗️ Architecture

```
  ┌──────────────────────────────────┐     ┌──────────────────────────────────┐
  │          User A  (Sender)        │     │         User B  (Receiver)       │
  │                                  │     │                                  │
  │  ┌──────────────────────────┐    │     │    ┌──────────────────────────┐  │
  │  │  PyQt6 GUI               │    │     │    │  PyQt6 GUI               │  │
  │  │  • File drop zone        │    │     │    │  • Accept dialog         │  │
  │  │  • AES-256-GCM encrypt   │    │     │    │  • AES-256-GCM decrypt   │  │
  │  │  • Progress bar          │    │     │    │  • Progress bar          │  │
  │  └────────────┬─────────────┘    │     │    └──────────────┬───────────┘  │
  │               │ IPC              │     │                   │ IPC          │
  │               │ TCP :1488        │     │         TCP :1488 │              │
  │  ┌────────────▼─────────────┐    │     │    ┌──────────────▼───────────┐  │
  │  │  holepunch  (Go binary)  │    │     │    │  holepunch  (Go binary)  │  │
  │  │  • p2p.Engine (pion)     │    │     │    │  • p2p.Engine (pion)     │  │
  │  │  • ICE / DTLS / SCTP     │    │     │    │  • ICE / DTLS / SCTP     │  │
  │  │  • IPC server            │    │     │    │  • IPC server            │  │
  │  └────────────┬─────────────┘    │     │    └──────────────┬───────────┘  │
  └───────────────┼──────────────────┘     └───────────────────┼──────────────┘
                  │                                             │
                  │ POST /signal/123-456/sender                 │
                  │                                             │ GET /signal/123-456/sender
                  │           ┌─────────────────┐              │
                  └──────────►│  Signal Server  │◄─────────────┘
                              │  Cloud Run      │
                              │  sync.Map       │
                              │  HTTP long-poll │
                              └────────┬────────┘
                                       │ SDP exchange complete
                                       │
          ◄════════════════════════════╧════════════════════════════►
                       Direct WebRTC DataChannel  (UDP)
                    encrypted chunks travel here, bypassing server
```

### Data flow for a file transfer

```
[Python Sender]          [Go holepunch A]       [Go holepunch B]     [Python Receiver]
      │                        │                       │                     │
      │── TypeConnect ─────────►│                       │                     │
      │                        │── POST offer SDP ─────►(signal server)       │
      │                        │                       │◄── GET offer SDP ───│
      │                        │◄── GET answer SDP ────│                     │
      │◄── status: connected ──│◄══ DataChannel open ══►│── status: connected►│
      │                        │                       │                     │
      │── metadata JSON ───────►│══ text frame ══════════►│── metadata JSON ──►│
      │                        │                       │◄── ready JSON ──────│
      │◄── ready JSON ─────────│◄══ text frame ═════════│                     │
      │                        │                       │                     │
      │── chunk[0] ────────────►│══ binary frame ════════►│── chunk[0] ────────►│
      │── chunk[1] ────────────►│══ binary frame ════════►│── chunk[1] ────────►│
      │        ···             │        ···             │        ···          │
```

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.11+** with pip
- **Go 1.22+** — [download](https://go.dev/dl/)

### Step 1 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Build the Go backend

**Windows:**
```bat
scripts\build.bat
```

**macOS / Linux:**
```bash
chmod +x scripts/build.sh
./scripts/build.sh
```

This places platform binaries in `bin/`. You only need to do this once (or after updating the Go code).

### Step 3 — Start a signaling server

For local testing, run the signal server in a separate terminal:

```bash
# macOS / Linux — built by build.sh above
./bin/signal-server-linux-amd64

# Windows — use WSL or Docker
docker run --rm -p 8080:8080 \
  $(docker build -q -f core/cmd/signal-server/Dockerfile core)
```

For production, deploy once to Cloud Run — see [Deploying the Signal Server](#-deploying-the-signal-server).

> **☁️ Using a Cloud Run signal server?**
> Once deployed, skip the local server entirely. Just point the app at your Cloud Run URL before launching:
>
> **Windows (PowerShell):**
> ```powershell
> $env:HOLEPUNCH_SIGNAL_URL = "https://your-signal-server.run.app"
> python main.py
> ```
>
> Or create a `.env` file in the project root (it's git-ignored):
> ```
> HOLEPUNCH_SIGNAL_URL=https://your-signal-server.run.app
> ```
>
> You can also paste the URL directly in the app: **Settings → Signal Server URL → Apply & Restart**

### Step 4 — Run

```bash
python main.py
```

Python auto-discovers the binary in `bin/`, spawns it, waits for the IPC port to open, and connects. The status indicator in the bottom-left of the sidebar turns green when the backend is live.

### Step 5 — Transfer a file

1. Click **Connect to Peer**
2. **Sender** → click *Send (Generate Code)* — a 6-digit code appears (e.g. `347-821`)
3. **Receiver** → click *Receive (Input Code)* — enter the code from the sender
4. Both sides enter the **same E2EE password** (min 8 chars)
5. Sender drags a file into the drop zone and clicks **INITIATE TRANSFER**

The file travels end-to-end encrypted directly between the two machines.

---

## 🔐 Security

HolePunch uses a layered encryption model. The Go backend never has access to the encryption key or the plaintext — it only ever sees opaque binary blobs.

| Layer | Algorithm | Notes |
|---|---|---|
| Key derivation | PBKDF2-HMAC-SHA256 | 100 000 iterations, hardware-resistant |
| KDF salt | 6-digit peer code | Shared out-of-band by voice/text; never sent over the network |
| Encryption | AES-256-GCM | Per-chunk authenticated encryption |
| Nonce | 12 bytes, random | Fresh `os.urandom(12)` per chunk — nonce reuse is structurally impossible |
| Transport | WebRTC DTLS 1.2 | Additional layer on top of AES-GCM; Go ↔ Go link is doubly encrypted |
| Auth tag | 128-bit GCM tag | Corrupt or tampered chunks are rejected before being written to disk |

### Threat model

- ✅ **Network attacker** — cannot decrypt or tamper with file data (two encryption layers)
- ✅ **Signal server operator** — sees only SDP metadata (~4 KB of ICE/codec info); never sees file data or the encryption key
- ✅ **Wrong password** — decryption fails on the first chunk; partial corrupt file is deleted automatically
- ⚠️ **Peer code sharing** — the 6-digit code is the KDF salt, not a secret key; a brute-force of a weak password is theoretically possible if the code is also known. Use a strong password (≥ 12 chars)
- ⚠️ **Local machine** — the plaintext exists briefly on the sender's disk and in Python memory during encryption; if the sender's machine is compromised this cannot be mitigated at the transport layer

---

## 🛠️ Building from Source

### Build all platforms at once

```bash
./scripts/build.sh          # all 6 holepunch binaries + signal-server
./scripts/build.sh windows  # windows only
./scripts/build.sh darwin   # macOS only
./scripts/build.sh linux    # linux only
./scripts/build.sh signal   # signal-server only (linux/amd64)
```

Outputs:
```
bin/
├── holepunch-windows-amd64.exe
├── holepunch-windows-arm64.exe
├── holepunch-darwin-amd64
├── holepunch-darwin-arm64
├── holepunch-linux-amd64
├── holepunch-linux-arm64
└── signal-server-linux-amd64
```

### Build a specific binary manually

```bash
cd core

# Current platform (Go picks GOOS/GOARCH automatically)
go build -o ../bin/holepunch-windows-amd64.exe ./cmd/holepunch

# Cross-compile from any host
GOOS=darwin GOARCH=arm64 go build -o ../bin/holepunch-darwin-arm64 ./cmd/holepunch
```

### Run tests

```bash
cd core
go test ./...
go vet ./...
```

---

## ☁️ Deploying the Signal Server

The signal server is a ~6 MiB stateless HTTP binary. Google Cloud Run's free tier (2 million requests/month) is more than enough for personal use.

### One-command deploy

**macOS / Linux:**

```bash
export GCP_PROJECT_ID=my-gcp-project
./scripts/deploy-signal.sh
```

The script will:
1. Enable the required GCP APIs
2. Build and push the Docker image via Cloud Build (no local Docker needed)
3. Deploy to Cloud Run with zero-minimum-instances (scales to zero when idle)
4. Print the public HTTPS URL and smoke-test the `/health` endpoint

**Windows (Command Prompt):**

```cmd
set GCP_PROJECT_ID=your-gcp-project
scripts\deploy-signal.bat
```

Or manually step by step:

```cmd
REM Step 1: Build Docker image (from core\ directory)
cd core
gcloud builds submit . --project your-gcp-project
cd ..

REM Step 2: Deploy to Cloud Run
gcloud run deploy holepunch-signal ^
    --image gcr.io/your-gcp-project/holepunch-signal:latest ^
    --platform managed --region us-central1 ^
    --allow-unauthenticated --port 8080 ^
    --memory 256Mi --cpu 1 ^
    --min-instances 0 --max-instances 20 ^
    --project your-gcp-project --quiet

REM Step 3: Get your URL
gcloud run services describe holepunch-signal ^
    --platform managed --region us-central1 ^
    --project your-gcp-project ^
    --format "value(status.url)"
```

### Configure the app to use your server

**macOS / Linux:**

```bash
# Option A: environment variable (recommended)
export HOLEPUNCH_SIGNAL_URL=https://holepunch-signal-xxxxxxxxxx-uc.a.run.app

# Option B: edit main.py
SIGNAL_URL = "https://holepunch-signal-xxxxxxxxxx-uc.a.run.app"
```

**Windows:**

```cmd
REM Option A: environment variable (Windows)
set HOLEPUNCH_SIGNAL_URL=https://holepunch-signal-xxxxxxxxxx-uc.a.run.app

REM Option B: .env file in project root (git-ignored)
echo HOLEPUNCH_SIGNAL_URL=https://holepunch-signal-xxxxxxxxxx-uc.a.run.app > .env

REM Option C: paste URL in the app GUI
REM Settings tab → Signal Server URL → Apply & Restart
```

### Manual Docker build

```bash
cd core
docker build -f cmd/signal-server/Dockerfile -t holepunch-signal .
docker run --rm -p 8080:8080 holepunch-signal
```

### Signal server API

| Method | Path | Body / Response | Description |
|---|---|---|---|
| `POST` | `/signal/{code}/{role}` | `{"sdp":"..."}` → 200 | Publish SDP for this role |
| `GET` | `/signal/{code}/{role}` | `{"sdp":"..."}` | Long-poll (≤ 60 s) until SDP available |
| `GET` | `/health` | `ok` | Health check for load balancers |

Sessions are automatically expired after **10 minutes** of inactivity.

---

## ⚙️ Configuration

### Python (`main.py`)

| Variable | Default | Description |
|---|---|---|
| `SIGNAL_URL` | `http://localhost:8080` | Signaling broker base URL. Override with `HOLEPUNCH_SIGNAL_URL` env var, `.env` file, or **Settings → Signal Server URL** in the GUI |
| `IPC_ADDR` | `127.0.0.1:1488` | TCP address the Go backend listens on |

### Go binary (`holepunch`) flags

```
--ipc-addr    string   IPC listen address              (default "127.0.0.1:1488")
--signal-url  string   Signaling broker base URL       (default "http://localhost:8080")
--stun        string   STUN server URI, comma-sep      (default "stun:stun.l.google.com:19302")
```

### Signal server flags

```
--port  string   TCP port to listen on  (default "8080", overridden by $PORT)
```

`$PORT` is set automatically by Google Cloud Run. No flag changes are needed for Cloud Run deployment.

---

## 📁 Project Structure

```
holepunch/
│
├── main.py                         # Entry point — spawns Go, connects IPC
├── requirements.txt                # Python dependencies
├── INTERFACE.md                    # IPC wire protocol specification
│
├── src/                            # Python application logic
│   ├── __init__.py
│   ├── gui.py                      # PyQt6 window, dialogs, drop zone
│   ├── cipher.py                   # AES-256-GCM encrypt / decrypt threads
│   └── ipc_link.py                 # IPC client thread (Python ↔ Go TCP)
│
├── assets/                         # Static files and themes
│   ├── style.qss                   # Dark theme stylesheet
│   └── style_light.qss             # Light theme stylesheet
│
├── scripts/                        # Build and deployment scripts
│   ├── build.sh                    
│   ├── build.bat                   
│   └── deploy-signal.sh            
│
├── bin/                            # Compiled binaries (git-ignored)
│   ├── .gitkeep
│   ├── holepunch-windows-amd64.exe
│   ├── holepunch-darwin-arm64
│   └── ...
│
└── core/                           # Go module (github.com/user/holepunch-core)
    ├── go.mod
    ├── go.sum
    │
    ├── cmd/
    │   ├── holepunch/
    │   │   └── main.go             # IPC server entry point, SIGTERM handler
    │   └── signal-server/
    │       ├── main.go             # HTTP signaling broker
    │       └── Dockerfile          # Multi-stage scratch image for Cloud Run
    │
    └── internal/
        ├── models/
        │   └── models.go           # Shared types: IPC messages, P2P envelope
        │
        ├── ipc/
        │   ├── protocol.go         # Length-prefix frame codec (ReadMsg/WriteMsg)
        │   └── server.go           # TCP server: Python ↔ Engine bridge
        │
        ├── p2p/
        │   ├── engine.go           # WebRTC PeerConnection lifecycle, SDP negotiation
        │   └── data_channel.go     # DataTransport: buffered recv, sync send
        │
        └── signaling/
            ├── signaler.go         # Signaler interface
            └── http_signaler.go    # HTTP long-poll implementation
```

---

## 🔌 IPC Protocol

Full specification: [`INTERFACE.md`](INTERFACE.md)

Every message between Python and Go is framed with a **4-byte big-endian length prefix**, identical to Python's `struct.pack('>I', n)`. Go uses `encoding/binary.BigEndian.Uint32` on the same header.

```
┌──────────────────────┬──────────────────────────────────────┐
│  Length (4 bytes BE) │  Payload (N bytes)                   │
└──────────────────────┴──────────────────────────────────────┘
```

Payload is either a UTF-8 JSON control message or a raw binary encrypted chunk. Python distinguishes the two with a `try/except json.loads` heuristic; Go uses `json.Unmarshal` for the same purpose.

---

## 🧩 How Local Testing Works (mock_router.py)

Before the full Go backend existed, `mock_router.py` acted as a TCP pipe between two Python instances running on the same machine. It still works for testing the Python-only encryption/decryption path:

```bash
# Terminal 1 — the pipe
python mock_router.py

# Terminal 2 — sender
python main.py

# Terminal 3 — receiver
python main.py
```

Connect one instance as Sender and the other as Receiver pointing at the same `localhost:1488`. Useful for validating the AES-GCM layer without touching WebRTC.

---

## 🤝 Contributing

1. **Fork** the repository and create a feature branch.
2. **Go changes** — run `go build ./...` and `go vet ./...` before committing. All new packages must compile cleanly with zero vet warnings.
3. **Python changes** — verify `main.py` launches without error and the IPC indicator turns green.
4. **New Go dependencies** — update `go.mod` / `go.sum` with `go mod tidy` and include them in the PR.
5. Open a **pull request** with a clear description of what changed and why.

### Coding conventions

- Go: standard `gofmt` formatting; `log/slog` for all structured logging; no `panic` outside of `main`.
- Python: [PEP 8](https://peps.python.org/pep-0008/); type hints on all public functions; no bare `except:` clauses.
- Commit messages: imperative mood, `<scope>: <what>` format (e.g. `ipc: add read deadline on context cancel`).

---

## 📋 Roadmap

- [ ] Resume interrupted transfers (chunk sequence numbers)
- [x] Multi-file / folder transfer (zip on the fly) - Done via .tar.gz streaming
- [ ] TURN server fallback for networks that block UDP entirely
- [x] Progress indication on the receiver side (requires metadata size field relay) - Done using the size field in metadata and calculations in the chunk handler
- [ ] Packaged installers (PyInstaller + bundled binary)
- [ ] End-to-end integration tests with two holepunch instances in CI

---

## 📄 License

MIT © 2024 HolePunch Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
