"""Main application window."""
from __future__ import annotations
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QScrollArea, QGroupBox, QLabel, QLineEdit, QSpinBox,
    QPushButton, QSplitter, QTextEdit, QTabWidget,
    QStatusBar, QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSlot, QThread, QObject, pyqtSignal
from PyQt6.QtGui import QAction

from pa_hardware import (
    LaserController, MockLaserController,
    OscilloscopeController, MockOscilloscopeController,
    PicoScope5444DController, MockPicoScope5444DController,
    StageController, MockStageController,
)
from .laser_widget import LaserWidget
from .oscilloscope_widget import OscilloscopeWidget
from .scan_widget import ScanWidget

_SCOPE_NAMES = {
    "redpitaya": "Red Pitaya STEM 125-10",
    "picoscope": "PicoScope 5444D MSO",
}


class _LaserStatusWorker(QObject):
    """Polls LaserController.get_status() on a background thread — each poll
    is several serial round-trips (pycobolt rate-limits to ~100 ms apart), so
    running it on the UI thread would visibly stall the GUI."""

    status_ready = pyqtSignal(dict)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, laser, poll_interval_s: float = 2.0):
        super().__init__()
        self._laser = laser
        self._interval = poll_interval_s
        # Set here, not in run(): stop() may be called before the thread's
        # event loop actually dispatches run(), and run() must not clobber it.
        self._running = True

    @pyqtSlot()
    def run(self):
        while self._running:
            try:
                status = self._laser.get_status()
                self.status_ready.emit(status)
            except Exception as exc:
                self.error.emit(str(exc))
                break
            elapsed = 0.0
            while self._running and elapsed < self._interval:
                time.sleep(0.1)
                elapsed += 0.1
        self.finished.emit()

    def stop(self):
        self._running = False


class MainWindow(QMainWindow):
    def __init__(self, mock: bool = False, scope_type: str = "redpitaya"):
        super().__init__()
        self._mock = mock
        self._scope_type = scope_type

        # Hardware instances
        self._laser = MockLaserController() if mock else LaserController()
        self._stage = MockStageController() if mock else StageController()
        self._scope = self._make_scope(mock, scope_type)

        self._laser_status_thread: QThread | None = None
        self._laser_status_worker: _LaserStatusWorker | None = None

        scope_label = _SCOPE_NAMES.get(scope_type, scope_type)
        title = f"PA Setup Control — {scope_label}"
        if mock:
            title += " [MOCK]"
        self.setWindowTitle(title)
        self.setMinimumSize(1100, 650)
        self._setup_ui()
        self._setup_menu()

        if mock:
            self._log(f"Mock mode active — {scope_label} simulated.")
            self._auto_connect_mock()

    @staticmethod
    def _make_scope(mock: bool, scope_type: str):
        if scope_type == "picoscope":
            return MockPicoScope5444DController() if mock else PicoScope5444DController()
        return MockOscilloscopeController() if mock else OscilloscopeController()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        # Built right-panel-first so self._scope_widget (and its
        # axis_controls) exists before _build_left_panel places it there —
        # splitter.addWidget order below still controls the visual layout.
        right_panel = self._build_right_panel()
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(right_panel)
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
        self._laser_widget.trigger_source_changed.connect(self._on_laser_trigger_source)
        self._laser_widget.rate_changed.connect(self._on_laser_rate)
        self._laser_widget.enable_changed.connect(self._on_laser_enable)
        self._laser_widget.clear_fault_requested.connect(self._on_laser_clear_fault)
        layout.addWidget(self._laser_widget)

        # Scope's axis controls live here rather than in the (already
        # crowded) scope panel on the right — there's more room on this side.
        layout.addWidget(self._scope_widget.axis_controls)

        layout.addWidget(self._build_log_panel())
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(500)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connections")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        # Laser row
        laser_row = QHBoxLayout()
        laser_row.addWidget(QLabel("Laser port:"))
        self._laser_port = QLineEdit("/dev/ttyACM0")
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

        # Stage row
        stage_row = QHBoxLayout()
        stage_row.addWidget(QLabel("Stage port:"))
        self._stage_port = QLineEdit("/dev/ttyUSB0")
        self._stage_port.setFixedWidth(70)
        stage_row.addWidget(self._stage_port)
        stage_row.addWidget(QLabel("Addr:"))
        self._stage_address = QSpinBox()
        self._stage_address.setRange(1, 99)
        self._stage_address.setValue(2)
        self._stage_address.setFixedWidth(45)
        stage_row.addWidget(self._stage_address)
        self._btn_stage_connect = QPushButton("Connect")
        self._btn_stage_connect.setFixedWidth(80)
        self._btn_stage_connect.clicked.connect(self._on_stage_connect)
        stage_row.addWidget(self._btn_stage_connect)
        self._lbl_stage_status = QLabel("●")
        self._lbl_stage_status.setStyleSheet("color: gray;")
        stage_row.addWidget(self._lbl_stage_status)
        layout.addLayout(stage_row)

        # Scope row — label shows which model is configured
        scope_row = QHBoxLayout()
        scope_label = _SCOPE_NAMES.get(self._scope_type, self._scope_type)
        scope_row.addWidget(QLabel(f"Scope ({scope_label}):"))
        scope_row.addStretch()
        self._btn_scope_connect = QPushButton("Connect")
        self._btn_scope_connect.setFixedWidth(80)
        self._btn_scope_connect.clicked.connect(self._on_scope_connect)
        scope_row.addWidget(self._btn_scope_connect)
        self._lbl_scope_status = QLabel("●")
        self._lbl_scope_status.setStyleSheet("color: gray;")
        scope_row.addWidget(self._lbl_scope_status)
        layout.addLayout(scope_row)

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

        self._scan_widget = ScanWidget(self._stage, self._scope, self._scope_widget)
        self._scan_widget.log_message.connect(self._log)

        tabs = QTabWidget()
        tabs.addTab(self._scope_widget, "Oscilloscope")
        tabs.addTab(self._scan_widget, "Scan")
        layout.addWidget(tabs)
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
        self._start_laser_status_polling()
        self._stage.connect(self._stage_port.text(), address=self._stage_address.value())
        self._set_status(self._lbl_stage_status, True)
        self._btn_stage_connect.setText("Disconnect")
        self._set_status(self._lbl_scope_status, True)

    # ------------------------------------------------------------------
    # Connection slots
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_laser_connect(self) -> None:
        if self._laser.is_connected:
            self._stop_laser_status_polling()
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
                self._start_laser_status_polling()
            except Exception as e:
                self._log(f"Laser connection failed: {e}")

    @pyqtSlot()
    def _on_stage_connect(self) -> None:
        if self._stage.is_connected:
            self._stage.disconnect()
            self._set_status(self._lbl_stage_status, False)
            self._btn_stage_connect.setText("Connect")
            self._log("Stage disconnected.")
        else:
            try:
                self._stage.connect(
                    self._stage_port.text(),
                    address=self._stage_address.value(),
                )
                self._set_status(self._lbl_stage_status, True)
                self._btn_stage_connect.setText("Disconnect")
                self._log(
                    f"Stage connected on {self._stage_port.text()} "
                    f"(address {self._stage_address.value()})."
                )
            except Exception as e:
                self._log(f"Stage connection failed: {e}")

    @pyqtSlot()
    def _on_scope_connect(self) -> None:
        scope_label = _SCOPE_NAMES.get(self._scope_type, self._scope_type)
        # Continuous acquisition runs in a child process that holds the
        # scope connection; get it back before checking/changing it here.
        self._scope_widget.stop_acquisition()
        if self._scope.is_connected:
            self._scope.disconnect()
            self._scope_widget.set_scope(self._scope)
            self._set_status(self._lbl_scope_status, False)
            self._btn_scope_connect.setText("Connect")
            self._log("Scope disconnected.")
        else:
            try:
                self._scope.connect()
                self._scope_widget.set_scope(self._scope)
                self._set_status(self._lbl_scope_status, True)
                self._btn_scope_connect.setText("Disconnect")
                self._log(f"{scope_label} connected.")
            except Exception as e:
                self._log(f"Scope connection failed: {e}")

    # ------------------------------------------------------------------
    # Laser status polling
    # ------------------------------------------------------------------

    def _start_laser_status_polling(self) -> None:
        self._stop_laser_status_polling()
        self._laser_status_thread = QThread()
        self._laser_status_worker = _LaserStatusWorker(self._laser)
        self._laser_status_worker.moveToThread(self._laser_status_thread)
        self._laser_status_thread.started.connect(self._laser_status_worker.run)
        self._laser_status_worker.status_ready.connect(self._on_laser_status)
        self._laser_status_worker.error.connect(self._on_laser_status_error)
        self._laser_status_worker.finished.connect(self._laser_status_thread.quit)
        self._laser_status_thread.start()

    def _stop_laser_status_polling(self) -> None:
        if self._laser_status_worker is not None:
            self._laser_status_worker.stop()
        if self._laser_status_thread is not None:
            self._laser_status_thread.quit()
            if not self._laser_status_thread.wait(2000):
                # Still running — stop() only takes effect on the next loop
                # iteration, but get_status() is several sequential blocking
                # serial round-trips that can still be in flight. Wait for
                # real completion instead of dropping the QThread reference
                # while its OS thread is still alive (crashes as "QThread:
                # Destroyed while thread is still running").
                self._laser_status_thread.wait()
        self._laser_status_thread = None
        self._laser_status_worker = None

    @pyqtSlot(dict)
    def _on_laser_status(self, status: dict) -> None:
        self._laser_widget.update_status(status)

    @pyqtSlot(str)
    def _on_laser_status_error(self, message: str) -> None:
        self._log(f"Laser status polling error: {message}")
        self._stop_laser_status_polling()

    # ------------------------------------------------------------------
    # Laser slots
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_laser_clear_fault(self) -> None:
        try:
            self._laser.clear_fault()
            self._log("Laser fault cleared.")
        except Exception as e:
            self._log(f"Laser clear_fault error: {e}")

    @pyqtSlot(str)
    def _on_laser_trigger_source(self, source: str) -> None:
        try:
            self._laser.set_trigger_source(source)
            self._log(f"Laser trigger source set to {source}.")
        except Exception as e:
            self._log(f"Laser set_trigger_source error: {e}")

    @pyqtSlot(float)
    def _on_laser_rate(self, rate_hz: float) -> None:
        try:
            self._laser.set_internal_rate(rate_hz)
        except Exception as e:
            self._log(f"Laser set_internal_rate error: {e}")

    @pyqtSlot(bool)
    def _on_laser_enable(self, enabled: bool) -> None:
        try:
            self._laser.set_enabled(enabled)
            state = "ON" if enabled else "OFF"
            self._log(f"Laser emission {state}.")
            self._status_bar.showMessage(f"Laser {state}")
        except Exception as e:
            self._log(f"Laser enable error: {e}")

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
        scope_label = _SCOPE_NAMES.get(self._scope_type, self._scope_type)
        QMessageBox.about(
            self,
            "PA Setup Control",
            f"Laser · Stage · Oscilloscope control GUI\n\n"
            f"Instruments:\n"
            f"  • Cobolt laser (USB serial)\n"
            f"  • Zaber T-LLS105 translation stage (USB serial)\n"
            f"  • {scope_label}",
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._stop_laser_status_polling()
        self._scope_widget.stop_acquisition()
        self._scan_widget.shutdown()
        for dev in (self._laser, self._stage, self._scope):
            try:
                dev.disconnect()
            except Exception:
                pass
        event.accept()
