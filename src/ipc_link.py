import socket
import json
import struct
import time
import threading
import weakref
from PyQt6.QtCore import QThread, pyqtSignal

class IPCClientThread(QThread):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    json_received = pyqtSignal(dict)
    chunk_received = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self, port=9999):
        super().__init__()
        self.port = port
        self.sock = None
        self.running = False
        self._lock = threading.Lock() # Додано м'ютекс для потокобезпеки
        # Safety net: ensure socket is closed if stop() is never called
        self._finalizer = weakref.finalize(self, self._finalize_socket, self.port)

    @staticmethod
    def _finalize_socket(port):
        # This runs if the object is garbage collected without stop() being called
        pass  # Socket cleanup is handled in stop() and the run loop's finally block

    def run(self):
        self.running = True
        while self.running:
            with self._lock:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                self.sock.connect(('127.0.0.1', self.port))
                self.connected.emit()

                while self.running:
                    raw_msglen = self.recvall(4)
                    if not raw_msglen:
                        break
                    
                    msglen = struct.unpack('>I', raw_msglen)[0]
                    
                    data = self.recvall(msglen)
                    if not data:
                        break

                    # TODO: Для надійності варто перейти на байт-префікс (напр. 0x01 - JSON, 0x02 - Chunk)
                    try:
                        text = data.decode('utf-8')
                        msg = json.loads(text)
                        self.json_received.emit(msg)
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        self.chunk_received.emit(data)

            except ConnectionRefusedError:
                pass 
            except OSError:
                break 
            except Exception as e:
                self.error.emit(f"IPC Error: {str(e)}")
            finally:
                with self._lock:
                    if self.sock:
                        self.sock.close()
                        self.sock = None
            
            if self.running:
                self.disconnected.emit()
                time.sleep(2) 

    def recvall(self, n):
        data = bytearray(n)  # Pre-allocate exact size needed
        view = memoryview(data)
        pos = 0
        while pos < n:
            try:
                # Перевіряємо наявність сокета без блокування, щоб уникнути помилок при розриві
                if not self.sock:
                    return None
                nbytes = self.sock.recv_into(view[pos:], n - pos)
                if nbytes == 0:
                    return None
                pos += nbytes
            except OSError:
                return None
        return bytes(data)

    def send_json(self, data: dict):
        payload = json.dumps(data).encode('utf-8')
        self._send_payload(payload)

    def send_chunk(self, data: bytes):
        self._send_payload(data)

    def _send_payload(self, payload: bytes):
        with self._lock: # Блокуємо доступ до сокета
            if not self.running or not self.sock:
                return
            
            header = struct.pack('>I', len(payload))
            try:
                self.sock.sendall(header + payload)
            except Exception as e:
                self.error.emit(f"Send failed: {str(e)}")

    def stop(self):
        self.running = False
        with self._lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                self.sock.close()
                self.sock = None

    def wait(self, timeout=5000):
        """Wait for the thread to finish, with a timeout in milliseconds."""
        super().wait(timeout)

    def __del__(self):
        """Safety net: ensure socket is closed if stop() was never called."""
        if self.sock is not None:
            try:
                with self._lock:
                    if self.sock:
                        self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                with self._lock:
                    if self.sock:
                        self.sock.close()
                        self.sock = None
            except Exception:
                pass