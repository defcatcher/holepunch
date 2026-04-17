import atexit
import json
import os
import platform
import socket
import secrets
import subprocess
import sys
import threading
import time
import appdirs
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.cipher import FileDecryptor, FileEncryptorThread, FolderEncryptorThread
from src.gui import P2PWindow
from src.ipc_link import IPCClientThread

def get_path_size(path: str) -> int:
    """Returns file size or total size of all files in a directory."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total

def format_speed_eta(bytes_per_sec: float, eta_seconds: float) -> tuple[str, str]:
    """Formats raw speed and ETA into human-readable strings."""
    if bytes_per_sec < 1024 * 1024:
        speed_str = f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        speed_str = f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        
    if eta_seconds < 60:
        eta_str = f"{int(eta_seconds)}s"
    elif eta_seconds < 3600:
        eta_str = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
    else:
        eta_str = f"{int(eta_seconds // 3600)}h {int((eta_seconds % 3600) // 60)}m"
        
    return speed_str, eta_str

APP_NAME = "HolePunch"
APP_AUTHOR = "HolePunchDevs"
config_dir = Path(appdirs.user_config_dir(APP_NAME, APP_AUTHOR))
CONFIG_FILE = config_dir / "config.json"

def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_config(data: dict) -> None:
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        
        existing = _load_config()
        existing.update(data)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save config: {e}")


SIGNAL_URL: str = (
    os.environ.get("HOLEPUNCH_SIGNAL_URL")
    or _load_config().get("signal_url", "")
    or "http://localhost:8080"
)

_saved_port = _load_config().get("ipc_port", 1488)
IPC_ADDR: str = f"127.0.0.1:{_saved_port}"

_go_proc: subprocess.Popen | None = None


def _find_binary() -> str:
    bin_dir = Path(__file__).parent / "bin"
    system = platform.system()
    machine = platform.machine()

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
    try:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            sys.stderr.buffer.write(raw_line)
            sys.stderr.buffer.flush()
    except Exception:
        pass 


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_go_backend() -> None:
    global _go_proc

    binary = _find_binary()
    host, port_str = IPC_ADDR.rsplit(":", 1)

    cmd = [binary, "--ipc-addr", IPC_ADDR, "--signal-url", SIGNAL_URL]

    extra: dict = {}
    if platform.system() == "Windows":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW

    _go_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **extra,
    )

    threading.Thread(
        target=_drain_go_stderr,
        args=(_go_proc,),
        daemon=True,
        name="go-stderr-drain",
    ).start()

    if not _wait_for_port(host, int(port_str), timeout=10.0):
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
    global _go_proc
    if _go_proc is None or _go_proc.poll() is not None:
        return 
    try:
        _go_proc.terminate() 
        _go_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _go_proc.kill()
        _go_proc.wait()
    finally:
        _go_proc = None

atexit.register(_stop_go_backend)


class AppController:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.view = P2PWindow()
        self.peer_ready = False
        self.is_sender = False
        self.is_receiver = False
        self.transfer_pending = False
        self.total_transfer_size = 0
        self.transfer_start_time = 0.0

        self.view.drop_zone.file_dropped.connect(self.on_file_selected)
        self.view.connect_btn.clicked.connect(self.view.show_connect_dialog)
        self.view.peer_connected.connect(self.on_peer_ready)
        self.view.start_btn.clicked.connect(self.run_transfer)
        self.view.cancel_btn.clicked.connect(self.cancel_transfer)
        self.view.btn_apply_port.clicked.connect(self.change_ipc_port)
        self.view.window_closing.connect(self.on_window_closing)
        self.view.port_input.setText(str(_saved_port))

        saved_dir = _load_config().get("default_dir", "")
        if saved_dir:
            self.view.path_input.setText(saved_dir)

        self.view.path_input.textChanged.connect(
            lambda text: _save_config({"default_dir": text})
        )

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

        _, port_str = IPC_ADDR.rsplit(":", 1)
        self.init_ipc(int(port_str))

        self.view.load_signal_url(
            SIGNAL_URL if SIGNAL_URL != "http://localhost:8080" else ""
        )
        self.view.btn_apply_signal.clicked.connect(self._on_apply_signal_url)

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
            _save_config({"ipc_port": new_port})
            self.view.status_ipc.setText("🔌 Backend: Reconnecting…")
            self.view.status_ipc.setStyleSheet(
                "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
            )
            self.ipc.stop()
            self.ipc.wait(5000)
            self.init_ipc(new_port)

    def apply_signal_url(self, url: str) -> None:
        global SIGNAL_URL
        try:
            _save_config({"signal_url": url})
            SIGNAL_URL = url
            self.view.status_ipc.setText("🔌 Backend: Restarting…")
            self.view.status_ipc.setStyleSheet(
                "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
            )
            self.ipc.stop()
            self.ipc.wait(5000)
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
        if hasattr(self, "selected_file") and self.peer_ready:
            self.view.start_btn.setEnabled(True)

    def on_file_selected(self, path: str) -> None:
        self.selected_file = path
        self.view.file_info.setText(f"Selected: {os.path.basename(path)}")
        if "Connected" in self.view.status_peer.text():
            self.view.start_btn.setEnabled(True)

    def on_peer_ready(self, code: str) -> None:
        self.peer_ready = True

        if code.startswith("HOST:"):
            actual_code = code[len("HOST:") :]
            self.view.active_code = actual_code
            self.is_sender = True
            self.is_receiver = False
            self.ipc.send_json(
                {
                    "type": "connect",
                    "code": actual_code,
                    "role": "sender",
                }
            )
        else:
            self.is_receiver = True
            self.is_sender = False
            self.ipc.send_json(
                {
                    "type": "connect",
                    "code": code,
                    "role": "receiver",
                }
            )

        self.view.update_transfer_status("peer_connecting")

    def reconnect_peer(self) -> None:
        if not self.is_sender:
            return

        self.view.reconnect_btn.setVisible(False)
        self.view.start_btn.setEnabled(False)

        new_code = f"{secrets.randbelow(900) + 100:03d}-{secrets.randbelow(900) + 100:03d}"
        self.view.my_peer_code = new_code
        self.view.active_code = new_code

        self.view.connect_btn.setText(f"Your Code: {new_code}")
        self.view.file_info.setText("🔄 Generating new session...")
        self.view.update_transfer_status("peer_connecting")
        self.view.status_peer.setText("👤 Peer: Waiting...")
        self.view.status_peer.setStyleSheet("color: #f1c40f; padding: 5px 10px; font-size: 11px;")
        self.ipc.send_json({
            "type": "connect",
            "code": new_code,
            "role": "sender",
        })

    def run_transfer(self) -> None:
        if not hasattr(self, "selected_file"):
            self.view.file_info.setText("❌ Error: No file selected!")
            return

        pwd = self.view.pwd_input.text()
        if len(pwd) < 8:
            self.view.file_info.setText("❌ Error: Password must be at least 8 chars!")
            return

        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)

        self.view.start_btn.setEnabled(False)
        self.transfer_pending = True
        self.view.file_info.setText("📤 Sending file info… waiting for receiver…")
        self.view.update_transfer_status("metadata_sent")

        self.total_transfer_size = get_path_size(self.selected_file)
        name = os.path.basename(self.selected_file)
        if os.path.isdir(self.selected_file):
            name += ".tar"

        self.ipc.send_json(
            {
                "type": "metadata",
                "name": name,
                "size": self.total_transfer_size,
            }
        )

    def on_ipc_json(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "ready":
            if self.transfer_pending:
                self.transfer_pending = False
                self.start_encryption()
        elif msg_type == "metadata":
            self.handle_incoming_metadata(msg)
        elif msg_type == "error":
            self.handle_remote_error(msg.get("msg", "unknown error"))
        elif msg_type == "status":
            self._handle_p2p_status(msg.get("value", ""))

    def _handle_p2p_status(self, value: str) -> None:
        if value == "connecting":
            self.view.status_peer.setText("👤 Peer: Connecting…")
            self.view.status_peer.setStyleSheet(
                "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
            )
            self.view.update_transfer_status("peer_connecting")
        elif value == "connected":
            self.view.status_peer.setText("👤 Peer: Connected ✓")
            self.view.status_peer.setStyleSheet(
                "color: #2ecc71; padding: 5px 10px; font-size: 11px; font-weight: bold;"
            )
            self.view.update_transfer_status("peer_connected")
            self.view.reconnect_btn.setVisible(False)
            if hasattr(self, "selected_file"):
                self.view.start_btn.setEnabled(True)
        elif value == "disconnected":
            self.view.status_peer.setText("👤 Peer: Disconnected")
            self.view.status_peer.setStyleSheet(
                "color: #e74c3c; padding: 5px 10px; font-size: 11px;"
            )
            self.view.update_transfer_status("disconnected")
            self.transfer_pending = False
            self.view.start_btn.setEnabled(False)
            if self.view.active_code and self.is_sender:
                self.view.reconnect_btn.setVisible(True)
        elif value == "finished":
            self.view.status_peer.setText("👤 Peer: Transfer complete")
            self.view.status_peer.setStyleSheet(
                "color: #2ecc71; padding: 5px 10px; font-size: 11px;"
            )
            self.view.update_transfer_status("transfer_complete")
        elif value == "error":
            self.view.status_peer.setText("👤 Peer: Error")
            self.view.status_peer.setStyleSheet(
                "color: #e74c3c; padding: 5px 10px; font-size: 11px;"
            )
            self.view.update_transfer_status("error")
            self.transfer_pending = False
            self.view.start_btn.setEnabled(False)
            if self.view.active_code and self.is_sender:
                self.view.reconnect_btn.setVisible(True)

    def handle_remote_error(self, err_msg: str) -> None:
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)  # Wait for thread to finish (5s timeout)

        save_path = None
        if hasattr(self, "decryptor"):
            save_path = self.decryptor.save_path
            self.decryptor.close()
            del self.decryptor

        self.transfer_pending = False
        self.view.file_info.setText(f"❌ Error: {err_msg}")
        self.view.update_transfer_status("error")
        self.view.start_btn.setEnabled(False)
        if self.view.active_code and self.is_sender:
            self.view.reconnect_btn.setVisible(True)
        self.view.progress_bar.setValue(0)

        if save_path and os.path.exists(save_path):
            reply = QMessageBox.question(
                self.view,
                "Delete incomplete file?",
                f"An error occurred. Delete the corrupted/incomplete file?\n{os.path.basename(save_path)}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    os.remove(save_path)
                except Exception:
                    pass

    def cancel_transfer(self) -> None:
        if hasattr(self, "ipc") and getattr(self.ipc, "running", False):
            self.ipc.send_json({"type": "error", "msg": "Transfer cancelled by user."})

        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)

        save_path = None
        if hasattr(self, "decryptor"):
            save_path = self.decryptor.save_path
            self.decryptor.close()
            del self.decryptor

        self.transfer_pending = False
        self.view.file_info.setText("Transfer cancelled.")
        self.view.update_transfer_status("error")
        self.view.start_btn.setEnabled(True)
        self.view.progress_bar.setValue(0)

        if save_path and os.path.exists(save_path):
            reply = QMessageBox.question(
                self.view,
                "Delete incomplete file?",
                f"Transfer was cancelled. Delete the incomplete file?\n{os.path.basename(save_path)}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    os.remove(save_path)
                    self.view.show_notification("Deleted", "Incomplete file deleted successfully.")
                except Exception as e:
                    print(f"Failed to delete: {e}")

    def handle_incoming_metadata(self, msg: dict) -> None:
        filename = msg.get("name", "unknown_file")
        filesize = msg.get("size", 0)
        current_pwd = self.view.pwd_input.text()
        default_dir = self.view.path_input.text()

        if filesize < 1024:
            size_str = f"{filesize} B"
        elif filesize < 1024 * 1024:
            size_str = f"{round(filesize / 1024, 2)} KB"
        elif filesize < 1024 * 1024 * 1024:
            size_str = f"{round(filesize / (1024 * 1024), 2)} MB"
        else:
            size_str = f"{round(filesize / (1024 * 1024 * 1024), 2)} GB"

        self.view.file_info.setText(f"📥 Incoming: {filename} ({size_str})")
        self.view.update_transfer_status("metadata_received")

        pwd, save_path = self.view.ask_receive_file(
            filename, filesize, current_pwd, default_dir
        )
        if not pwd or not save_path:
            self.view.file_info.setText("❌ Transfer rejected.")
            self.view.update_transfer_status("rejected")
            self.ipc.send_json({"type": "error", "msg": "Transfer rejected by user"})
            return

        pin_code = self.view.active_code
        try:
            self.decryptor = FileDecryptor(save_path, pwd, pin_code)
            self.expected_size = filesize
            self.received_size = 0
            self.transfer_start_time = time.time()
            self.view.file_info.setText(f"📥 Receiving {filename}…")
            self.view.update_transfer_status("receiving")
            self.view.progress_bar.setValue(0)
            QTimer.singleShot(500, lambda: self.ipc.send_json({"type": "ready"}))
        except Exception as exc:
            self.view.file_info.setText(f"❌ Decryption setup error: {exc}")
            self.view.update_transfer_status("error")
            self.ipc.send_json({"type": "error", "msg": f"Decryption setup error: {exc}"})

    def on_ipc_chunk(self, data: bytes) -> None:
        if not hasattr(self, "decryptor"):
            return

        if not self.decryptor.process_chunk(data):
            self.view.file_info.setText("❌ Wrong Password or PIN mismatch!")
            self.view.update_transfer_status("decryption_error")
            save_path = self.decryptor.save_path
            self.decryptor.close()
            del self.decryptor
            if os.path.exists(save_path):
                os.remove(save_path)
            self.ipc.send_json(
                {"type": "error", "msg": "Wrong Password or PIN mismatch"}
            )
            return

        self.received_size += max(0, len(data) - 28)
        if self.expected_size > 0:
            percent = int((self.received_size / self.expected_size) * 100)

            now = time.time()
            if now - getattr(self, 'last_ui_update_time', 0) > 0.5:
                self.last_ui_update_time = now
                self.view.progress_bar.setValue(min(100, percent))

                elapsed = now - self.transfer_start_time
                if elapsed > 0:
                    speed = self.received_size / elapsed
                    eta = (self.expected_size - self.received_size) / speed if speed > 0 else 0
                    speed_str, eta_str = format_speed_eta(speed, eta)
                    received_display = self._format_size(self.received_size)
                    expected_display = self._format_size(self.expected_size)
                    self.view.file_info.setText(
                        f"📥 {percent}% | {speed_str} | ETA: {eta_str} ({received_display}/{expected_display})"
                    )
                    self.view.update_transfer_status("receiving")

            if self.received_size >= self.expected_size:
                self.view.file_info.setText("✅ Successfully received!")
                self.view.update_transfer_status("transfer_complete")
                self.view.progress_bar.setValue(100)
                self.view.show_notification("Transfer Complete", "File has been successfully received.")
                self.decryptor.close()
                del self.decryptor

    def start_encryption(self) -> None:
        self.view.file_info.setText("🔒 Encrypting and sending…")
        self.view.update_transfer_status("sending")
        pwd = self.view.pwd_input.text()
        pin_code = self.view.active_code

        self.transfer_start_time = time.time()

        if os.path.isdir(self.selected_file):
            self.worker = FolderEncryptorThread(self.selected_file, pwd, pin_code, self.total_transfer_size)
        else:
            self.worker = FileEncryptorThread(self.selected_file, pwd, pin_code)

        self.worker.progress.connect(self.on_send_progress)
        self.worker.chunk_ready.connect(self.send_chunk_to_go, Qt.ConnectionType.DirectConnection)
        self.worker.finished.connect(self.on_transfer_complete)
        self.worker.error.connect(self.on_transfer_error)
        self.worker.start()

    def on_send_progress(self, processed_size) -> None:
        if self.total_transfer_size > 0:
            percent = int((processed_size / self.total_transfer_size) * 100)

            now = time.time()
            if now - getattr(self, 'last_ui_update_time', 0) > 0.5:
                self.last_ui_update_time = now
                self.view.progress_bar.setValue(min(100, percent))

                elapsed = now - self.transfer_start_time
                if elapsed > 0:
                    speed = processed_size / elapsed  # Effective throughput (useful data)
                    remaining = self.total_transfer_size - processed_size
                    eta = remaining / speed if speed > 0 else 0
                    speed_str, eta_str = format_speed_eta(speed, eta)
                    self.view.file_info.setText(f"📤 {percent}% | {speed_str} | ETA: {eta_str}")
                    self.view.update_transfer_status("sending")

    def send_chunk_to_go(self, chunk_data: bytes) -> None:
        self.ipc.send_chunk(chunk_data)

    def on_transfer_complete(self) -> None:
        self.view.file_info.setText("✅ Successfully transmitted!")
        self.view.update_transfer_status("transfer_complete")
        self.view.progress_bar.setValue(100)
        self.view.start_btn.setEnabled(True)
        self.view.show_notification("Transfer Complete", "File has been successfully sent.")

    def on_transfer_error(self, err_msg: str) -> None:
        self.view.file_info.setText(f"❌ Error: {err_msg}")
        self.view.update_transfer_status("error")
        self.view.start_btn.setEnabled(True)

    def on_window_closing(self) -> None:
        """Handle window close event to ensure orderly shutdown of all resources."""
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)

        if hasattr(self, "decryptor"):
            try:
                self.decryptor.close()
            except Exception:
                pass

        if hasattr(self, "ipc"):
            self.ipc.stop()
            self.ipc.wait(5000)

    def _shutdown(self) -> None:
        """Shut down all resources orderly."""
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)

        if hasattr(self, "decryptor"):
            try:
                self.decryptor.close()
            except Exception:
                pass

        if hasattr(self, "ipc"):
            self.ipc.stop()
            self.ipc.wait(5000)

        _stop_go_backend()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format byte size into human-readable string."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{round(size_bytes / 1024, 2)} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{round(size_bytes / (1024 * 1024), 2)} MB"
        else:
            return f"{round(size_bytes / (1024 * 1024 * 1024), 2)} GB"

    def run(self) -> None:
        self.view.show()
        exit_code = self.app.exec()
        self._shutdown()
        sys.exit(exit_code)


if __name__ == "__main__":
    ctrl = AppController()
    ctrl.run()