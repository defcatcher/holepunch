import os
from PyQt6.QtCore import QThread, pyqtSignal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidTag

CHUNK_SIZE = 64 * 1024

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
            file_size = os.path.getsize(self.file_path)
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
                    percent = int((processed_size / file_size) * 100)
                    self.progress.emit(percent)

            if self.is_running:
                self.finished.emit()

        except Exception as e:
            self.error.emit(str(e))

    def stop(self):
        self.is_running = False

class FileDecryptor:
    def __init__(self, save_path: str, password: str, pin_code: str):
        self.save_path = save_path
        self.key = generate_key(password, pin_code)
        self.aesgcm = AESGCM(self.key)
        self.file = open(self.save_path, 'wb')

    def process_chunk(self, payload: bytes) -> bool:
        nonce = payload[:12]
        ciphertext = payload[12:]
        try:
            decrypted_chunk = self.aesgcm.decrypt(nonce, ciphertext, None)
            self.file.write(decrypted_chunk)
            return True
        except InvalidTag:
            return False

    def close(self):
        if not self.file.closed:
            self.file.close()