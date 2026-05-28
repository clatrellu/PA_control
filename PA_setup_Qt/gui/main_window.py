"""Main application window."""
from __future__ import annotations
from datetime import datetime

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QScrollArea, QGroupBox, QLabel, QLineEdit,
    QPushButton, QComboBox, QSplitter, QTextEdit,
    QStatusBar, QMenuBar, QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSlot
from PyQt6.QtGui import QAction

from pa_hardware import (
    LaserController, MockLaserController,
    GalvoController, MockGalvoController,
    OscilloscopeController, MockOscilloscopeController,
    TriggerController, MockTriggerController,
)
from gui.laser_widget import LaserWidget
from gui.galvo_widget import GalvoWidget
from gui.oscilloscope_widget import OscilloscopeWidget
from gui.trigger_widget import TriggerWidget


class MainWindow(QMainWindow):
    def __init__(self, mock: bool = False):
        super().__init__()
        self._mock = mock

        # Hardware instances
        self._laser   = MockLaserController()   if mock else LaserController()
        self._galvo   = MockGalvoController()   if mock else GalvoController()
        self._scope   = MockOscilloscopeController() if mock else OscilloscopeController()
        self._trigger = MockTriggerController() if mock else TriggerController()

        self.setWindowTitle("PA Setup Control" + (" [MOCK]" if mock else ""))
        self.setMinimumSize(1100, 650)
        self._setup_ui()
        self._setup_menu()

        if mock:
            self._log("Mock mode active — no hardware required.")
            self._auto_connect_mock()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 780])

        root.addWidget(splitter)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

    def _build_left_panel(self) -> QScrollArea:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(self._build_connection_group())

        self._laser_widget = LaserWidget()
        self._laser_widget.power_changed.connect(self._on_laser_power)
        self._laser_widget.enable_changed.connect(self._on_laser_enable)
        self._laser_widget.mode_changed.connect(self._on_laser_mode)
        layout.addWidget(self._laser_widget)

        self._galvo_widget = GalvoWidget()
        self._galvo_widget.position_changed.connect(self._on_galvo_move)
        self._galvo_widget.center_requested.connect(self._on_galvo_center)
        layout.addWidget(self._galvo_widget)

        self._trigger_widget = TriggerWidget()
        self._trigger_widget.start_requested.connect(self._on_trigger_start)
        self._trigger_widget.stop_requested.connect(self._on_trigger_stop)
        layout.addWidget(self._trigger_widget)

        layout.addWidget(self._build_log_panel())
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(330)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connections")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        # Laser row
        laser_row = QHBoxLayout()
        laser_row.addWidget(QLabel("Laser port:"))
        self._laser_port = QLineEdit("COM3")
        self._laser_port.setFixedWidth(70)
        laser_row.addWidget(self._laser_port)
        self._btn_laser_connect = QPushButton("Connect")
        self._btn_laser_connect.setFixedWidth(80)
        self._btn_laser_connect.clicked.connect(self._on_laser_connect)
        laser_row.addWidget(self._btn_laser_connect)
        self._lbl_laser_status = QLabel("●")
        self._lbl_laser_status.setStyleSheet("color: gray;")
        laser_row.addWidget(self._lbl_laser_status)
        layout.addLayout(laser_row)

        # Galvo row
        galvo_row = QHBoxLayout()
        galvo_row.addWidget(QLabel("X ch:"))
        self._galvo_x_ch = QLineEdit("Dev1/ao0")
        self._galvo_x_ch.setFixedWidth(80)
        galvo_row.addWidget(self._galvo_x_ch)
        galvo_row.addWidget(QLabel("Y:"))
        self._galvo_y_ch = QLineEdit("Dev1/ao1")
        self._galvo_y_ch.setFixedWidth(80)
        galvo_row.addWidget(self._galvo_y_ch)
        layout.addLayout(galvo_row)

        galvo_btn_row = QHBoxLayout()
        galvo_btn_row.addStretch()
        self._btn_galvo_connect = QPushButton("Connect Galvo")
        self._btn_galvo_connect.setFixedWidth(110)
        self._btn_galvo_connect.clicked.connect(self._on_galvo_connect)
        galvo_btn_row.addWidget(self._btn_galvo_connect)
        self._lbl_galvo_status = QLabel("●")
        self._lbl_galvo_status.setStyleSheet("color: gray;")
        galvo_btn_row.addWidget(self._lbl_galvo_status)
        layout.addLayout(galvo_btn_row)

        # Scope row
        scope_row = QHBoxLayout()
        scope_row.addStretch()
        self._btn_scope_connect = QPushButton("Connect Scope")
        self._btn_scope_connect.setFixedWidth(110)
        self._btn_scope_connect.clicked.connect(self._on_scope_connect)
        scope_row.addWidget(self._btn_scope_connect)
        self._lbl_scope_status = QLabel("●")
        self._lbl_scope_status.setStyleSheet("color: gray;")
        scope_row.addWidget(self._lbl_scope_status)
        layout.addLayout(scope_row)

        # Trigger row
        trigger_row = QHBoxLayout()
        trigger_row.addStretch()
        self._btn_trigger_connect = QPushButton("Connect Trigger")
        self._btn_trigger_connect.setFixedWidth(115)
        self._btn_trigger_connect.clicked.connect(self._on_trigger_connect)
        trigger_row.addWidget(self._btn_trigger_connect)
        self._lbl_trigger_status = QLabel("●")
        self._lbl_trigger_status.setStyleSheet("color: gray;")
        trigger_row.addWidget(self._lbl_trigger_status)
        layout.addLayout(trigger_row)

        return group

    def _build_log_panel(self) -> QGroupBox:
        group = QGroupBox("Log")
        layout = QVBoxLayout(group)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setFixedHeight(130)
        self._log_box.setStyleSheet("font-family: monospace; font-size: 10px;")
        layout.addWidget(self._log_box)
        return group

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self._scope_widget = OscilloscopeWidget(self._scope)
        self._scope_widget.log_message.connect(self._log)
        layout.addWidget(self._scope_widget)
        return panel

    def _setup_menu(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = bar.addMenu("Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------
    # Mock auto-connect
    # ------------------------------------------------------------------

    def _auto_connect_mock(self) -> None:
        self._set_status(self._lbl_laser_status, True)
        self._laser_widget.set_connected(True)
        self._set_status(self._lbl_galvo_status, True)
        self._galvo_widget.set_connected(True)
        self._set_status(self._lbl_scope_status, True)
        self._trigger.connect("Dev1/ctr0")
        self._set_status(self._lbl_trigger_status, True)
        self._trigger_widget.set_connected(True)

    # ------------------------------------------------------------------
    # Connection slots
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_laser_connect(self) -> None:
        if self._laser.is_connected:
            self._laser.disconnect()
            self._set_status(self._lbl_laser_status, False)
            self._laser_widget.set_connected(False)
            self._btn_laser_connect.setText("Connect")
            self._log("Laser disconnected.")
        else:
            try:
                self._laser.connect(self._laser_port.text())
                self._set_status(self._lbl_laser_status, True)
                self._laser_widget.set_connected(True)
                self._btn_laser_connect.setText("Disconnect")
                self._log(f"Laser connected on {self._laser_port.text()}.")
            except Exception as e:
                self._log(f"Laser connection failed: {e}")

    @pyqtSlot()
    def _on_galvo_connect(self) -> None:
        if self._galvo.is_connected:
            self._galvo.disconnect()
            self._set_status(self._lbl_galvo_status, False)
            self._galvo_widget.set_connected(False)
            self._btn_galvo_connect.setText("Connect Galvo")
            self._log("Galvo disconnected.")
        else:
            try:
                self._galvo.connect(
                    self._galvo_x_ch.text(),
                    self._galvo_y_ch.text(),
                )
                self._set_status(self._lbl_galvo_status, True)
                self._galvo_widget.set_connected(True)
                self._btn_galvo_connect.setText("Disconnect Galvo")
                self._log(
                    f"Galvo connected (X={self._galvo_x_ch.text()}, "
                    f"Y={self._galvo_y_ch.text()})."
                )
            except Exception as e:
                self._log(f"Galvo connection failed: {e}")

    @pyqtSlot()
    def _on_scope_connect(self) -> None:
        if self._scope.is_connected:
            self._scope.disconnect()
            self._scope_widget.set_scope(self._scope)
            self._set_status(self._lbl_scope_status, False)
            self._btn_scope_connect.setText("Connect Scope")
            self._log("Scope disconnected.")
        else:
            try:
                self._scope.connect()
                self._scope_widget.set_scope(self._scope)
                self._set_status(self._lbl_scope_status, True)
                self._btn_scope_connect.setText("Disconnect Scope")
                self._log("PicoScope connected.")
            except Exception as e:
                self._log(f"Scope connection failed: {e}")

    # ------------------------------------------------------------------
    # Laser slots
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_trigger_connect(self) -> None:
        if self._trigger.is_connected:
            self._trigger.disconnect()
            self._set_status(self._lbl_trigger_status, False)
            self._trigger_widget.set_connected(False)
            self._btn_trigger_connect.setText("Connect Trigger")
            self._log("Trigger disconnected.")
        else:
            channel = self._trigger_widget.channel()
            try:
                self._trigger.connect(channel)
                self._set_status(self._lbl_trigger_status, True)
                self._trigger_widget.set_connected(True)
                self._btn_trigger_connect.setText("Disconnect Trigger")
                self._log(f"Trigger connected on {channel}.")
            except Exception as e:
                self._log(f"Trigger connection failed: {e}")

    # ------------------------------------------------------------------
    # Laser slots
    # ------------------------------------------------------------------

    @pyqtSlot(float)
    def _on_laser_power(self, mw: float) -> None:
        try:
            self._laser.set_power(mw)
        except Exception as e:
            self._log(f"Laser set_power error: {e}")

    @pyqtSlot(bool)
    def _on_laser_enable(self, enabled: bool) -> None:
        try:
            self._laser.set_enabled(enabled)
            state = "ON" if enabled else "OFF"
            self._log(f"Laser emission {state}.")
            self._status_bar.showMessage(f"Laser {state}")
        except Exception as e:
            self._log(f"Laser enable error: {e}")

    @pyqtSlot(str)
    def _on_laser_mode(self, mode: str) -> None:
        try:
            self._laser.set_modulation_mode(mode)
            self._log(f"Laser mode → {mode}.")
        except Exception as e:
            self._log(f"Laser mode error: {e}")

    # ------------------------------------------------------------------
    # Galvo slots
    # ------------------------------------------------------------------

    @pyqtSlot(float, float)
    def _on_galvo_move(self, x_v: float, y_v: float) -> None:
        try:
            self._galvo.move_to(x_v, y_v)
        except Exception as e:
            self._log(f"Galvo move error: {e}")

    @pyqtSlot(float, float)
    def _on_trigger_start(self, freq_hz: float, duty: float) -> None:
        try:
            self._trigger.start(freq_hz, duty)
            self._trigger_widget.set_running(True)
            self._log(f"Trigger started: {freq_hz:.1f} Hz, duty {duty*100:.1f} %.")
        except Exception as e:
            self._log(f"Trigger start error: {e}")

    @pyqtSlot()
    def _on_trigger_stop(self) -> None:
        try:
            self._trigger.stop()
            self._trigger_widget.set_running(False)
            self._log("Trigger stopped.")
        except Exception as e:
            self._log(f"Trigger stop error: {e}")

    @pyqtSlot()
    def _on_galvo_center(self) -> None:
        try:
            self._galvo.center()
            self._log("Galvo centered.")
        except Exception as e:
            self._log(f"Galvo center error: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, label: QLabel, ok: bool) -> None:
        label.setStyleSheet(f"color: {'#4CAF50' if ok else 'gray'};")

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_box.append(f"[{ts}] {msg}")
        self._status_bar.showMessage(msg)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "PA Setup Control",
            "Laser · Galvo · PicoScope control GUI\n\n"
            "Instruments:\n"
            "  • Cobolt laser (serial)\n"
            "  • Thorlabs galvo mirrors (NI-DAQ)\n"
            "  • PicoScope 5000 series",
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        for dev in (self._trigger, self._laser, self._galvo, self._scope):
            try:
                dev.disconnect()
            except Exception:
                pass
        event.accept()
