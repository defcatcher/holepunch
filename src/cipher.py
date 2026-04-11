import os
import tarfile
import weakref
from PyQt6.QtCore import QThread, pyqtSignal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidTag

CHUNK_SIZE = 60 * 1024

def generate_key(password: str, salt: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt.encode('utf-8'),
        iterations=100_000, 
    )
    return kdf.derive(password.encode('utf-8'))

class FileEncryptorThread(QThread):
    progress = pyqtSignal(int)
    chunk_ready = pyqtSignal(bytes)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, file_path: str, password: str, pin_code: str):
        super().__init__()
        self.file_path = file_path
        self.key = generate_key(password, pin_code)
        self.is_running = True

    def run(self):
        try:
            aesgcm = AESGCM(self.key)
            processed_size = 0

            with open(self.file_path, 'rb') as f:
                while self.is_running:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    nonce = os.urandom(12)
                    encrypted_chunk = aesgcm.encrypt(nonce, chunk, None)
                    payload = nonce + encrypted_chunk
                    
                    self.chunk_ready.emit(payload)

                    processed_size += len(chunk)
                    self.progress.emit(processed_size)

            if self.is_running:
                self.finished.emit()

        except Exception as e:
            self.error.emit(str(e))

    def stop(self):
        self.is_running = False

class EmitterStream:
    def __init__(self, encryptor_thread):
        self.thread = encryptor_thread
        self.buffer = bytearray()
        self.processed_size = 0
        self.max_buffer_size = CHUNK_SIZE * 4  # Prevent unbounded growth

    def write(self, data: bytes):
        if not self.thread.is_running:
            return len(data)

        self.buffer.extend(data)

        # Process chunks while buffer has enough data
        while len(self.buffer) >= CHUNK_SIZE and self.thread.is_running:
            chunk = bytes(self.buffer[:CHUNK_SIZE])
            del self.buffer[:CHUNK_SIZE]  # In-place deletion to avoid creating new bytearray

            nonce = os.urandom(12)
            encrypted_chunk = self.thread.aesgcm.encrypt(nonce, chunk, None)
            self.thread.chunk_ready.emit(nonce + encrypted_chunk)

            self.processed_size += len(chunk)
            self.thread.progress.emit(self.processed_size)

        # Safety check: if buffer gets too large, flush it all
        if len(self.buffer) > self.max_buffer_size:
            self.flush()

        return len(data)

    def flush(self):
        if self.buffer and self.thread.is_running:
            chunk = bytes(self.buffer)
            nonce = os.urandom(12)
            encrypted_chunk = self.thread.aesgcm.encrypt(nonce, chunk, None)
            self.thread.chunk_ready.emit(nonce + encrypted_chunk)
            self.processed_size += len(chunk)
            self.buffer.clear()
            self.thread.progress.emit(self.processed_size)

class FolderEncryptorThread(QThread):
    progress = pyqtSignal(int)
    chunk_ready = pyqtSignal(bytes)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, folder_path: str, password: str, pin_code: str, total_size: int):
        super().__init__()
        self.folder_path = folder_path
        self.key = generate_key(password, pin_code)
        self.aesgcm = AESGCM(self.key)
        self.total_size = total_size
        self.is_running = True

    def run(self):
        try:
            stream = EmitterStream(self)
            with tarfile.open(fileobj=stream, mode='w|') as tar:
                base_name = os.path.basename(self.folder_path)
                tar.add(self.folder_path, arcname=base_name)
            
            stream.flush()

            if self.is_running:
                self.finished.emit()

        except Exception as e:
            self.error.emit(f"Archiving error: {str(e)}")

    def stop(self):
        self.is_running = False

class FileDecryptor:
    def __init__(self, save_path: str, password: str, pin_code: str):
        self.save_path = save_path
        self.key = generate_key(password, pin_code)
        self.aesgcm = AESGCM(self.key)
        self.file = open(self.save_path, 'wb')
        self._closed = False
        # Use weakref.finalize as a safety net if close() is never called
        self._finalizer = weakref.finalize(self, self._finalize, self.file)

    @staticmethod
    def _finalize(file_obj):
        if not file_obj.closed:
            try:
                file_obj.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def process_chunk(self, payload: bytes) -> bool:
        if self._closed:
            return False
        nonce = payload[:12]
        ciphertext = payload[12:]
        try:
            decrypted_chunk = self.aesgcm.decrypt(nonce, ciphertext, None)
            self.file.write(decrypted_chunk)
            return True
        except InvalidTag:
            return False

    def close(self):
        if not self._closed and not self.file.closed:
            self.file.close()
            self._closed = True
            self._finalizer.atexit = False  # Disable finalizer since we closed manually
