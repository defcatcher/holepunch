import atexit
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.cipher import FileDecryptor, FileEncryptorThread
from src.gui import P2PWindow
from src.ipc_link import IPCClientThread

# ── Configuration ─────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    """Load persistent config from config.json if it exists."""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_config(data: dict) -> None:
    """Persist config to config.json."""
    try:
        existing = _load_config()
        existing.update(data)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save config: {e}")


# Base URL of the HolePunch signaling broker.
# Override with the HOLEPUNCH_SIGNAL_URL environment variable to point at your
# deployed Cloud Run instance without modifying source code.
SIGNAL_URL: str = (
    os.environ.get("HOLEPUNCH_SIGNAL_URL")
    or _load_config().get("signal_url", "")
    or "http://localhost:8080"
)

# TCP address the Go backend listens on for the Python IPC connection.
# Must match the --ipc-addr flag passed to the holepunch binary below.
IPC_ADDR: str = "127.0.0.1:1488"


# ── Go backend process management ─────────────────────────────────────────────

_go_proc: subprocess.Popen | None = None


def _find_binary() -> str:
    """Return the absolute path to the platform-appropriate holepunch binary.

    Binaries are expected in the ``bin/`` directory next to this script:

        bin/holepunch-windows-amd64.exe
        bin/holepunch-darwin-arm64
        bin/holepunch-linux-amd64
        ...

    Raises FileNotFoundError with a build hint if the binary is missing.
    """
    bin_dir = Path(__file__).parent / "bin"

    system = platform.system()  # "Windows" | "Darwin" | "Linux"
    machine = platform.machine()  # varies: "AMD64", "x86_64", "arm64", …

    # Normalise architecture names to the GOARCH convention.
    arch_map: dict[str, str] = {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "ARM64": "arm64",
    }
    arch = arch_map.get(machine, machine.lower())

    if system == "Windows":
        name = f"holepunch-windows-{arch}.exe"
    elif system == "Darwin":
        name = f"holepunch-darwin-{arch}"
    else:
        name = f"holepunch-linux-{arch}"

    candidate = bin_dir / name
    if candidate.exists():
        return str(candidate)

    raise FileNotFoundError(
        f"HolePunch backend binary not found:\n  {candidate}\n\n"
        f"Build it with:\n"
        f"  cd core\n"
        f"  go build -o ../bin/{name} ./cmd/holepunch\n"
    )


def _drain_go_stderr(proc: subprocess.Popen) -> None:
    """Drain the Go process stderr pipe in a daemon thread.

    Without draining, the OS pipe buffer (~64 KiB on Linux) will fill up when
    the Go backend emits structured logs during a long transfer, causing the
    Go process to block on its next slog write. Draining the pipe prevents
    this deadlock and also makes Go's log output visible in the terminal when
    main.py is run from a console.
    """
    try:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            sys.stderr.buffer.write(raw_line)
            sys.stderr.buffer.flush()
    except Exception:
        pass  # Process exited — pipe closed; nothing to do.


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll host:port until a TCP connection succeeds or *timeout* seconds pass.

    Returns True if the port accepted a connection, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_go_backend() -> None:
    """Spawn the holepunch Go binary and wait for its IPC port to open.

    On Windows the process is created without a visible console window.
    Raises FileNotFoundError if the binary cannot be found, or RuntimeError
    if the binary starts but the IPC port does not open within 10 seconds.
    """
    global _go_proc

    binary = _find_binary()
    host, port_str = IPC_ADDR.rsplit(":", 1)

    # Build the command.  Extra flags can be appended here or sourced from a
    # config file in future iterations.
    cmd = [binary, "--ipc-addr", IPC_ADDR, "--signal-url", SIGNAL_URL]

    # On Windows, suppress the flash of a black console window.
    extra: dict = {}
    if platform.system() == "Windows":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW

    _go_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **extra,
    )

    # Drain stderr in the background so the pipe never fills up.
    threading.Thread(
        target=_drain_go_stderr,
        args=(_go_proc,),
        daemon=True,
        name="go-stderr-drain",
    ).start()

    # Wait up to 10 s for the IPC port to become reachable.
    if not _wait_for_port(host, int(port_str), timeout=10.0):
        # The binary may have crashed immediately — read any buffered output.
        _go_proc.terminate()
        try:
            _go_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _go_proc.kill()

        raise RuntimeError(
            f"The Go backend did not open the IPC port ({IPC_ADDR}) within 10 s.\n"
            f"Check that no other process is using port {port_str} and that the "
            f"binary has execute permissions."
        )


def _stop_go_backend() -> None:
    """Terminate the Go backend gracefully.  Registered with atexit so it runs
    automatically when the Python process exits for any reason (window close,
    Ctrl-C, sys.exit, unhandled exception).

    Sequence:
      1. SIGTERM  — Go's signal handler cancels the root context and begins
                    a clean shutdown (closes PeerConnection, TCP listener).
      2. Wait up to 5 s for the process to exit on its own.
      3. SIGKILL  — if the process is still alive after the grace period.
    """
    global _go_proc
    if _go_proc is None or _go_proc.poll() is not None:
        return  # Already exited or was never started.
    try:
        _go_proc.terminate()  # SIGTERM on POSIX, TerminateProcess on Windows
        _go_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _go_proc.kill()
        _go_proc.wait()
    finally:
        _go_proc = None


# Register cleanup so the Go process is never left dangling.
atexit.register(_stop_go_backend)


# ── Application controller ─────────────────────────────────────────────────────


class AppController:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.view = P2PWindow()
        self.peer_ready = False

        # Wire GUI signals before we touch the network so no event is missed.
        self.view.drop_zone.file_dropped.connect(self.on_file_selected)
        self.view.connect_btn.clicked.connect(self.view.show_connect_dialog)
        self.view.peer_connected.connect(self.on_peer_ready)
        self.view.start_btn.clicked.connect(self.run_transfer)
        self.view.btn_apply_port.clicked.connect(self.change_ipc_port)

        # ── Spawn Go backend ──────────────────────────────────────────────────
        # This must happen before init_ipc so the TCP port is open when the
        # IPCClientThread attempts its first connection.
        try:
            _start_go_backend()
        except FileNotFoundError as exc:
            QMessageBox.critical(
                None,
                "Backend Binary Not Found",
                str(exc),
            )
            sys.exit(1)
        except RuntimeError as exc:
            QMessageBox.critical(
                None,
                "Backend Failed to Start",
                str(exc),
            )
            sys.exit(1)

        # Extract port from IPC_ADDR and connect the IPC client thread.
        _, port_str = IPC_ADDR.rsplit(":", 1)
        self.init_ipc(int(port_str))

        # Load persisted signal URL into the settings field and wire button.
        self.view.load_signal_url(
            SIGNAL_URL if SIGNAL_URL != "http://localhost:8080" else ""
        )
        self.view.btn_apply_signal.clicked.connect(self._on_apply_signal_url)

    # ── IPC lifecycle ──────────────────────────────────────────────────────────

    def init_ipc(self, port: int) -> None:
        self.ipc = IPCClientThread(port=port)
        self.ipc.connected.connect(self.on_ipc_connected)
        self.ipc.disconnected.connect(self.on_ipc_disconnected)
        self.ipc.json_received.connect(self.on_ipc_json)
        self.ipc.chunk_received.connect(self.on_ipc_chunk)
        self.ipc.start()

    def change_ipc_port(self) -> None:
        new_port = int(self.view.port_input.text())
        if new_port != self.ipc.port:
            self.view.status_ipc.setText("🔌 Backend: Reconnecting…")
            self.view.status_ipc.setStyleSheet(
                "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
            )
            self.ipc.stop()
            self.ipc.wait()
            self.init_ipc(new_port)

    def apply_signal_url(self, url: str) -> None:
        """Save *url* to config, restart the Go backend, and reconnect IPC."""
        global SIGNAL_URL
        try:
            _save_config({"signal_url": url})
            SIGNAL_URL = url
            self.view.status_ipc.setText("🔌 Backend: Restarting…")
            self.view.status_ipc.setStyleSheet(
                "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
            )
            self.ipc.stop()
            self.ipc.wait()
            _stop_go_backend()
            _start_go_backend()
            _, port_str = IPC_ADDR.rsplit(":", 1)
            self.init_ipc(int(port_str))
        except Exception as exc:
            QMessageBox.critical(
                self.view,
                "Signal URL Error",
                f"Failed to apply Signal URL:\n{exc}",
            )

    def _on_apply_signal_url(self) -> None:
        url = self.view.signal_url_input.text().strip()
        if not url:
            return
        self.apply_signal_url(url)

    def on_ipc_connected(self) -> None:
        self.view.status_ipc.setText(f"🔌 Backend: Connected ({self.ipc.port})")
        self.view.status_ipc.setStyleSheet(
            "color: #2ecc71; padding: 5px 10px; font-size: 11px;"
        )

    def on_ipc_disconnected(self) -> None:
        self.view.status_ipc.setText("🔌 Backend: Lost")
        self.view.status_ipc.setStyleSheet(
            "color: #e74c3c; padding: 5px 10px; font-size: 11px; font-weight: bold;"
        )
        # Unblock the UI so the user can retry if the backend crashed.
        if hasattr(self, "selected_file") and self.peer_ready:
            self.view.start_btn.setEnabled(True)

    # ── File selection ─────────────────────────────────────────────────────────

    def on_file_selected(self, path: str) -> None:
        self.selected_file = path
        self.view.file_info.setText(f"Selected: {os.path.basename(path)}")
        if self.peer_ready:
            self.view.start_btn.setEnabled(True)

    # ── Peer connection — sends TypeConnect to the Go backend ──────────────────

    def on_peer_ready(self, code: str) -> None:
        """Called by the GUI when the user sets up a peer connection.

        *code* is either ``"HOST:XXX-XXX"`` (we are the sender / offerer)
        or ``"XXX-XXX"`` (we are the receiver / answerer).  We parse the role
        from this value and dispatch a ``TypeConnect`` message to the Go backend
        so it can begin WebRTC negotiation through the signaling broker.
        """
        self.peer_ready = True

        if code.startswith("HOST:"):
            # We are the WebRTC offerer (sender).
            actual_code = code[len("HOST:") :]
            self.view.active_code = actual_code
            self.ipc.send_json(
                {
                    "type": "connect",
                    "code": actual_code,
                    "role": "sender",
                }
            )
        else:
            # We are the WebRTC answerer (receiver).
            self.ipc.send_json(
                {
                    "type": "connect",
                    "code": code,
                    "role": "receiver",
                }
            )

        if hasattr(self, "selected_file"):
            self.view.start_btn.setEnabled(True)

    # ── Transfer initiation ────────────────────────────────────────────────────

    def run_transfer(self) -> None:
        pwd = self.view.pwd_input.text()
        if len(pwd) < 8:
            self.view.file_info.setText("Error: Password must be at least 8 chars!")
            return

        self.view.start_btn.setEnabled(False)
        self.view.file_info.setText("Sending metadata… Waiting for peer.")

        self.ipc.send_json(
            {
                "type": "metadata",
                "name": os.path.basename(self.selected_file),
                "size": os.path.getsize(self.selected_file),
            }
        )

    # ── Inbound IPC messages from Go ───────────────────────────────────────────

    def on_ipc_json(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "ready":
            self.start_encryption()
        elif msg_type == "metadata":
            self.handle_incoming_metadata(msg)
        elif msg_type == "error":
            self.handle_remote_error(msg.get("msg", "unknown error"))
        elif msg_type == "status":
            self._handle_p2p_status(msg.get("value", ""))
        # TypeSignal and any future types are intentionally ignored here.

    def _handle_p2p_status(self, value: str) -> None:
        """Update the peer status indicator in the sidebar based on the P2P
        connection state reported by the Go backend."""
        if value == "connecting":
            self.view.status_peer.setText("👤 Peer: Connecting…")
            self.view.status_peer.setStyleSheet(
                "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
            )

        elif value == "connected":
            self.view.status_peer.setText("👤 Peer: Connected ✓")
            self.view.status_peer.setStyleSheet(
                "color: #2ecc71; padding: 5px 10px; font-size: 11px; font-weight: bold;"
            )

        elif value == "disconnected":
            self.view.status_peer.setText("👤 Peer: Disconnected")
            self.view.status_peer.setStyleSheet(
                "color: #e74c3c; padding: 5px 10px; font-size: 11px;"
            )
            self.view.start_btn.setEnabled(True)

        elif value == "finished":
            self.view.status_peer.setText("👤 Peer: Transfer complete")
            self.view.status_peer.setStyleSheet(
                "color: #2ecc71; padding: 5px 10px; font-size: 11px;"
            )

        elif value == "error":
            self.view.status_peer.setText("👤 Peer: Error")
            self.view.status_peer.setStyleSheet(
                "color: #e74c3c; padding: 5px 10px; font-size: 11px;"
            )
            self.view.start_btn.setEnabled(True)

    def handle_remote_error(self, err_msg: str) -> None:
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
        self.view.file_info.setText(f"Peer Error: {err_msg}")
        self.view.start_btn.setEnabled(True)

    def handle_incoming_metadata(self, msg: dict) -> None:
        filename = msg.get("name", "unknown_file")
        filesize = msg.get("size", 0)
        current_pwd = self.view.pwd_input.text()
        default_dir = self.view.path_input.text()

        pwd, save_path = self.view.ask_receive_file(
            filename, filesize, current_pwd, default_dir
        )
        if not pwd or not save_path:
            self.view.file_info.setText("Transfer rejected by user.")
            self.ipc.send_json({"type": "error", "msg": "Transfer rejected by user"})
            return

        pin_code = self.view.active_code
        try:
            self.decryptor = FileDecryptor(save_path, pwd, pin_code)
            self.expected_size = filesize
            self.received_size = 0
            self.view.file_info.setText(f"Receiving {filename}…")
            self.view.progress_bar.setValue(0)
            self.ipc.send_json({"type": "ready"})
        except Exception as exc:
            self.view.file_info.setText(f"Decryption setup error: {exc}")

    def on_ipc_chunk(self, data: bytes) -> None:
        if not hasattr(self, "decryptor"):
            return

        if not self.decryptor.process_chunk(data):
            self.view.file_info.setText("Error: Wrong Password or PIN mismatch")
            save_path = self.decryptor.save_path
            self.decryptor.close()
            del self.decryptor
            if os.path.exists(save_path):
                os.remove(save_path)
            self.ipc.send_json(
                {"type": "error", "msg": "Wrong Password or PIN mismatch"}
            )
            return

        # Each encrypted chunk carries 28 bytes of overhead (12 nonce + 16 GCM tag).
        self.received_size += max(0, len(data) - 28)
        if self.expected_size > 0:
            percent = int((self.received_size / self.expected_size) * 100)
            self.view.progress_bar.setValue(min(100, percent))

            if self.received_size >= self.expected_size:
                self.view.file_info.setText("Successfully received!")
                self.decryptor.close()
                del self.decryptor

    # ── Encryption / send pipeline ─────────────────────────────────────────────

    def start_encryption(self) -> None:
        self.view.file_info.setText("Encrypting and sending…")
        pwd = self.view.pwd_input.text()
        pin_code = self.view.active_code

        self.worker = FileEncryptorThread(self.selected_file, pwd, pin_code)
        self.worker.progress.connect(self.view.progress_bar.setValue)
        self.worker.chunk_ready.connect(self.send_chunk_to_go)
        self.worker.finished.connect(self.on_transfer_complete)
        self.worker.error.connect(self.on_transfer_error)
        self.worker.start()

    def send_chunk_to_go(self, chunk_data: bytes) -> None:
        self.ipc.send_chunk(chunk_data)

    def on_transfer_complete(self) -> None:
        self.view.file_info.setText("Successfully transmitted!")
        self.view.start_btn.setEnabled(True)

    def on_transfer_error(self, err_msg: str) -> None:
        self.view.file_info.setText(f"Error: {err_msg}")
        self.view.start_btn.setEnabled(True)

    # ── Run ────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.view.show()
        sys.exit(self.app.exec())


if __name__ == "__main__":
    ctrl = AppController()
    ctrl.run()
