import os
import secrets
import string

from PyQt6.QtCore import QRegularExpression, Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QSystemTrayIcon
)


class DropZone(QLabel):
    file_dropped = pyqtSignal(str)

    def __init__(self):
        super().__init__("📁\nDrop file here")
        self.setObjectName("DropZone")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setProperty("active", "false")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
            self.setProperty("active", "true")
            self.setStyle(self.style())
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setProperty("active", "false")
        self.setStyle(self.style())

    def dropEvent(self, event):
        self.setProperty("active", "false")
        self.setStyle(self.style())
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        if files:
            self.file_dropped.emit(files[0])


class CodeInputDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect")
        layout = QVBoxLayout(self)

        label = QLabel("Enter peer code:")
        self.input_field = QLineEdit()
        self.input_field.setInputMask("000-000")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(label)
        layout.addWidget(self.input_field)
        layout.addWidget(buttons)

    def get_code(self):
        return self.input_field.text()


class P2PWindow(QMainWindow):
    peer_connected = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("HolePunch")
        self.resize(800, 500)

        self.my_peer_code = (
            f"{secrets.randbelow(900) + 100:03d}-{secrets.randbelow(900) + 100:03d}"
        )

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        sidebar_layout = QVBoxLayout(self.sidebar)

        logo = QLabel("HolePunch")
        logo.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #3498db; padding: 10px;"
        )
        sidebar_layout.addWidget(logo)

        self.btn_send = QPushButton("📤 Send File")
        self.btn_send.setObjectName("NavBtn")
        self.btn_send.setProperty("selected", True)
        self.btn_settings = QPushButton("⚙️ Settings")
        self.btn_settings.setObjectName("NavBtn")
        self.btn_settings.setProperty("selected", False)

        sidebar_layout.addWidget(self.btn_send)
        sidebar_layout.addWidget(self.btn_settings)
        sidebar_layout.addStretch()

        self.status_ipc = QLabel("🔌 Backend: Lost")
        self.status_ipc.setStyleSheet(
            "color: #e74c3c; padding: 5px 10px; font-size: 11px;"
        )

        self.status_peer = QLabel("👤 Peer: No Code")
        self.status_peer.setStyleSheet(
            "color: #7f8c8d; padding: 5px 10px; font-size: 11px;"
        )

        # Transfer status log (detailed activity feed)
        self.transfer_log = QLabel()
        self.transfer_log.setObjectName("TransferLog")
        self.transfer_log.setWordWrap(True)
        self.transfer_log.setStyleSheet(
            "color: #95a5a6; padding: 5px 10px; font-size: 10px; "
            "border-top: 1px solid #34495e; margin-top: 5px;"
        )
        self.transfer_log.setText("📋 Activity log will appear here")
        self.transfer_log.setMinimumHeight(60)
        self.transfer_log.setMaximumHeight(100)

        sidebar_layout.addWidget(self.status_ipc)
        sidebar_layout.addWidget(self.status_peer)
        sidebar_layout.addWidget(self.transfer_log)

        self.content_area = QStackedWidget()

        self.init_transfer_page()
        self.init_settings_page()

        main_layout.addWidget(self.sidebar)
        main_layout.addWidget(self.content_area)

        self.btn_send.clicked.connect(
            lambda: self.switch_page(self.page_transfer, self.btn_send)
        )
        self.btn_settings.clicked.connect(
            lambda: self.switch_page(self.page_settings, self.btn_settings)
        )

        self.tray_icon = QSystemTrayIcon(self)
        icon = self.style().standardIcon(self.style().StandardPixmap.SP_DriveNetIcon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.show()

        self.change_theme("Dark (Default)")

    def init_transfer_page(self):
        self.page_transfer = QWidget()
        trans_layout = QVBoxLayout(self.page_transfer)
        trans_layout.setContentsMargins(30, 30, 30, 30)

        header = QHBoxLayout()
        self.title = QLabel("Transfer Engine")
        self.title.setStyleSheet("font-size: 20px; font-weight: bold;")
        self.connect_btn = QPushButton("Connect to Peer")
        self.connect_btn.setObjectName("ConnectBtn")
        header.addWidget(self.title)
        header.addStretch()
        header.addWidget(self.connect_btn)

        self.drop_zone = DropZone()
        self.file_info = QLabel("Ready to transmit...")
        self.progress_bar = QProgressBar()

        self.active_code = ""
        self.pwd_input = QLineEdit()
        self.pwd_input.setPlaceholderText("Enter E2EE Password (8-32 chars)")
        self.pwd_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_input.setMaxLength(32)

        regex = QRegularExpression(r"^[a-zA-Z0-9!@#$%^&*()_\-+=<>?]+$")
        validator = QRegularExpressionValidator(regex, self.pwd_input)
        self.pwd_input.setValidator(validator)

        self.start_btn = QPushButton("INITIATE TRANSFER")
        self.start_btn.setObjectName("StartBtn")
        self.start_btn.setEnabled(False)

        trans_layout.addLayout(header)
        trans_layout.addSpacing(20)
        trans_layout.addWidget(self.drop_zone)
        trans_layout.addWidget(self.file_info)
        trans_layout.addWidget(self.progress_bar)
        trans_layout.addWidget(self.pwd_input)
        trans_layout.addWidget(self.start_btn)
        self.content_area.addWidget(self.page_transfer)

    def init_settings_page(self):
        self.page_settings = QWidget()
        layout = QVBoxLayout(self.page_settings)
        layout.setContentsMargins(30, 30, 30, 30)

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(title)
        layout.addSpacing(20)

        signal_layout = QHBoxLayout()
        self.signal_url_input = QLineEdit()
        self.signal_url_input.setPlaceholderText(
            "https://holepunch-signal-xxxxxxxxxx-uc.a.run.app"
        )
        self.signal_url_input.setMinimumWidth(300)
        self.btn_apply_signal = QPushButton("Apply & Restart")
        self.btn_apply_signal.setObjectName("WarningBtn")
        signal_layout.addWidget(QLabel("Signal Server URL:"))
        signal_layout.addWidget(self.signal_url_input)
        signal_layout.addWidget(self.btn_apply_signal)
        layout.addLayout(signal_layout)
        layout.addSpacing(10)

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setReadOnly(True)
        self.path_input.setPlaceholderText("Default download directory...")

        self.btn_browse = QPushButton("Browse")
        self.btn_browse.setObjectName("ActionBtn")
        self.btn_browse.clicked.connect(self.choose_default_path)
        path_layout.addWidget(QLabel("Download Path:"))
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.btn_browse)
        layout.addLayout(path_layout)

        port_layout = QHBoxLayout()
        self.port_input = QLineEdit("1488")
        self.port_input.setValidator(QIntValidator(1, 65535))
        self.port_input.setFixedWidth(80)

        self.btn_apply_port = QPushButton("Apply Port")
        self.btn_apply_port.setObjectName("WarningBtn")
        port_layout.addWidget(QLabel("IPC Port:"))
        port_layout.addWidget(self.port_input)
        port_layout.addWidget(self.btn_apply_port)
        port_layout.addStretch()
        layout.addLayout(port_layout)

        btn_gen_pwd = QPushButton("🔑 Auto-Generate Secure Password")
        btn_gen_pwd.setObjectName("SuccessBtn")
        btn_gen_pwd.clicked.connect(self.generate_password)
        layout.addWidget(btn_gen_pwd)

        theme_layout = QHBoxLayout()
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark (Default)", "Light"])
        self.theme_combo.currentTextChanged.connect(self.change_theme)
        theme_layout.addWidget(QLabel("Theme:"))
        theme_layout.addWidget(self.theme_combo)
        theme_layout.addStretch()
        layout.addLayout(theme_layout)

        layout.addStretch()
        self.content_area.addWidget(self.page_settings)

    def show_connect_dialog(self):
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Connection Setup")
        msg_box.setText("Do you want to send or receive a file?")
        btn_send = msg_box.addButton(
            "Send (Generate Code)", QMessageBox.ButtonRole.ActionRole
        )
        btn_recv = msg_box.addButton(
            "Receive (Input Code)", QMessageBox.ButtonRole.ActionRole
        )
        msg_box.addButton(QMessageBox.StandardButton.Cancel)

        msg_box.exec()

        clicked = msg_box.clickedButton()
        if clicked == btn_send:
            self.display_my_code(self.my_peer_code)
            self.peer_connected.emit(f"HOST:{self.my_peer_code}")
        elif clicked == btn_recv:
            self.prompt_for_code()

    def display_my_code(self, code):
        self.connect_btn.setText(f"Your Code: {code}")
        self.status_peer.setText("👤 Peer: Waiting...")
        self.status_peer.setStyleSheet(
            "color: #f1c40f; padding: 5px 10px; font-size: 11px;"
        )
        self.active_code = code

    def prompt_for_code(self):
        dialog = CodeInputDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            code = dialog.get_code()
            if len(code) == 7:
                self.connect_btn.setText(f"Peer: {code}")
                self.status_peer.setText("👤 Peer: Target Set")
                self.status_peer.setStyleSheet(
                    "color: #3498db; padding: 5px 10px; font-size: 11px;"
                )
                self.active_code = code
                self.peer_connected.emit(code)

    def ask_receive_file(self, filename, size, default_pwd="", default_dir=""):
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{round(size / 1024, 2)} KB"
        elif size < 1024 * 1024 * 1024:
            size_str = f"{round(size / (1024 * 1024), 2)} MB"
        else:
            size_str = f"{round(size / (1024 * 1024 * 1024), 2)} GB"

        reply = QMessageBox.question(
            self,
            "Incoming Transfer",
            f"Accept file/folder: {filename}\nSize: {size_str}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.No:
            return None, None

        pwd = default_pwd
        if not pwd:
            pwd, ok = QInputDialog.getText(
                self, "Decryption", "Enter E2EE Password:", QLineEdit.EchoMode.Password
            )
            if not ok or not pwd:
                return None, None

        initial_path = os.path.join(default_dir, filename) if default_dir else filename
        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", initial_path)
        if not save_path:
            return None, None

        return pwd, save_path

    def choose_default_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Download Directory")
        if path:
            self.path_input.setText(path)

    def generate_password(self):
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        pwd = "".join(secrets.choice(alphabet) for _ in range(16))
        self.pwd_input.setText(pwd)
        QApplication.clipboard().setText(pwd)
        QMessageBox.information(
            self, "Copied", "Secure password generated and copied to clipboard!"
        )

    def switch_page(self, page, button):
        self.content_area.setCurrentWidget(page)
        self.set_active_nav(button)

    def set_active_nav(self, active_button):
        for btn in (self.btn_send, self.btn_settings):
            is_active = btn is active_button
            btn.setProperty("selected", is_active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()

    def change_theme(self, theme_name):
        file_name = "style_light.qss" if "Light" in theme_name else "style.qss"

        base_dir = os.path.dirname(os.path.dirname(__file__))
        style_path = os.path.join(base_dir, "assets", file_name)
        
        if os.path.exists(style_path):
            with open(style_path, "r") as f:
                self.setStyleSheet(f.read())
        else:
            print(f"Warning: Theme file {style_path} not found.")

    def load_signal_url(self, url: str):
        self.signal_url_input.setText(url)

    def update_transfer_status(self, status: str):
        """Update the transfer activity log in the sidebar with detailed status."""
        status_messages = {
            "peer_connecting": "🔄 Connecting to peer…",
            "peer_connected": "✅ Peer connected",
            "metadata_sent": "📤 File info sent, waiting for receiver…",
            "metadata_received": "📥 File info received",
            "receiving": "📥 Receiving file…",
            "sending": "📤 Sending file…",
            "transfer_complete": "✅ Transfer complete",
            "disconnected": "❌ Peer disconnected",
            "error": "❌ Error occurred",
            "rejected": "❌ Transfer rejected",
            "decryption_error": "❌ Decryption failed",
        }
        msg = status_messages.get(status, status)
        self.transfer_log.setText(msg)
        self.transfer_log.adjustSize()

    def load_styles(self):
        self.change_theme("Dark")
        
    def show_notification(self, title: str, message: str):
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)