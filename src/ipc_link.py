import socket
import json
import struct
import time
import threading
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
                    except (UnicodeDecodeError, json.JSONDecodeError):
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
        data = bytearray()
        while len(data) < n:
            try:
                # Перевіряємо наявність сокета без блокування, щоб уникнути помилок при розриві
                if not self.sock:
                    return None
                packet = self.sock.recv(n - len(data))
                if not packet:
                    return None
                data.extend(packet)
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