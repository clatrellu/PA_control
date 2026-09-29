"""Laser control panel widget."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QDoubleSpinBox, QComboBox, QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt, pyqtSignal


class LaserWidget(QGroupBox):
    """Controls for the Cobolt Tor XE: enable/disable, trigger source, and
    internal repetition rate (USB serial). The Tor XE has a fixed pulse
    energy — there is no power setpoint."""

    enable_changed = pyqtSignal(bool)
    trigger_source_changed = pyqtSignal(str)
    rate_changed = pyqtSignal(float)
    clear_fault_requested = pyqtSignal()

    MAX_RATE_HZ = 1000.0
    TRIGGER_SOURCES = ("internal", "external", "gated")

    def __init__(self, parent=None):
        super().__init__("Laser", parent)
        self._building = True
        self._setup_ui()
        self._building = False
        self.set_connected(False)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # --- Enable / disable row ---
        row = QHBoxLayout()
        self._btn_on = QPushButton("Emission ON")
        self._btn_on.setCheckable(True)
        self._btn_on.setFixedHeight(32)
        self._btn_on.setToolTip(
            "On a CDRH unit, turning emission OFF requires physically cycling\n"
            "the key switch before it will turn back on — the command that\n"
            "does that (@cob1) can't re-arm emission on its own.\n"
            "Use Trigger source to pause/resume pulses without touching this."
        )
        self._btn_on.toggled.connect(self._on_toggle)
        row.addWidget(self._btn_on)
        layout.addLayout(row)

        warning = QLabel(
            "⚠ Emission OFF requires a physical key cycle to resume on a CDRH "
            "unit. Use Trigger source to pause/resume pulses instead."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #E5A000; font-size: 10px;")
        layout.addWidget(warning)

        # --- Trigger source ---
        layout.addWidget(QLabel("Trigger source:"))
        self._combo_trigger = QComboBox()
        self._combo_trigger.addItems(self.TRIGGER_SOURCES)
        self._combo_trigger.currentTextChanged.connect(self._on_trigger_source_changed)
        layout.addWidget(self._combo_trigger)

        # --- Internal repetition rate ---
        layout.addWidget(QLabel("Internal rep. rate (Hz):"))
        self._spin_rate = QDoubleSpinBox()
        self._spin_rate.setRange(0.0, self.MAX_RATE_HZ)
        self._spin_rate.setSingleStep(10.0)
        self._spin_rate.setDecimals(0)
        self._spin_rate.setSuffix(" Hz")
        self._spin_rate.setValue(10.0)
        self._spin_rate.valueChanged.connect(self._on_rate_changed)
        layout.addWidget(self._spin_rate)

        # --- Measured repetition rate readback ---
        readback_row = QHBoxLayout()
        readback_row.addWidget(QLabel("Measured rep. rate:"))
        self._lbl_actual = QLabel("-- Hz")
        self._lbl_actual.setAlignment(Qt.AlignmentFlag.AlignRight)
        readback_row.addWidget(self._lbl_actual)
        layout.addLayout(readback_row)

        # --- Status section ---
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)
        layout.addWidget(QLabel("Status:"))

        self._lbl_ready = QLabel("--")
        self._lbl_ready.setStyleSheet("font-weight: bold;")

        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(10)
        r = 0
        status_grid.addWidget(QLabel("Ready:"), r, 0)
        status_grid.addWidget(self._lbl_ready, r, 1)
        r += 1

        self._lbl_state = QLabel("--")
        status_grid.addWidget(QLabel("Operating state:"), r, 0)
        status_grid.addWidget(self._lbl_state, r, 1)
        r += 1

        self._lbl_interlock = QLabel("--")
        status_grid.addWidget(QLabel("Interlock:"), r, 0)
        status_grid.addWidget(self._lbl_interlock, r, 1)
        r += 1

        self._lbl_fault = QLabel("--")
        self._btn_clear_fault = QPushButton("Clear Fault")
        self._btn_clear_fault.clicked.connect(self.clear_fault_requested.emit)
        status_grid.addWidget(QLabel("Fault:"), r, 0)
        status_grid.addWidget(self._lbl_fault, r, 1)
        status_grid.addWidget(self._btn_clear_fault, r, 2)
        r += 1

        self._lbl_autostart = QLabel("--")
        status_grid.addWidget(QLabel("Autostart enabled:"), r, 0)
        status_grid.addWidget(self._lbl_autostart, r, 1)
        r += 1

        self._lbl_leds = QLabel("--")
        status_grid.addWidget(QLabel("LEDs:"), r, 0)
        status_grid.addWidget(self._lbl_leds, r, 1, 1, 2)
        r += 1

        self._lbl_hours = QLabel("--")
        status_grid.addWidget(QLabel("Operating hours:"), r, 0)
        status_grid.addWidget(self._lbl_hours, r, 1)

        layout.addLayout(status_grid)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        self._btn_on.setEnabled(connected)
        self._combo_trigger.setEnabled(connected)
        self._spin_rate.setEnabled(connected)
        self._btn_clear_fault.setEnabled(connected)
        if not connected:
            self._btn_on.setChecked(False)
            self._lbl_actual.setText("-- Hz")
            self._lbl_ready.setText("--")
            self._lbl_ready.setStyleSheet("font-weight: bold;")
            self._lbl_state.setText("--")
            self._lbl_interlock.setText("--")
            self._lbl_fault.setText("--")
            self._lbl_autostart.setText("--")
            self._lbl_leds.setText("--")
            self._lbl_hours.setText("--")

    def update_repetition_rate(self, rate_hz: float) -> None:
        self._lbl_actual.setText(f"{rate_hz:.1f} Hz")

    def update_emission(self, enabled: bool) -> None:
        self._btn_on.blockSignals(True)
        self._btn_on.setChecked(enabled)
        self._btn_on.blockSignals(False)
        self._update_button_style(enabled)

    def update_trigger_source(self, source: str) -> None:
        self._combo_trigger.blockSignals(True)
        self._combo_trigger.setCurrentText(source)
        self._combo_trigger.blockSignals(False)

    def update_status(self, status: dict) -> None:
        """Refresh every readback from a LaserController.get_status() dict."""
        self.update_emission(status["enabled"])
        self.update_trigger_source(status["trigger_source"])
        self.update_repetition_rate(status["repetition_rate_hz"])

        ready = status["ready"]
        self._lbl_ready.setText("Ready" if ready else "Not ready")
        self._lbl_ready.setStyleSheet(
            "font-weight: bold; color: #4CAF50;" if ready else "font-weight: bold; color: #E53935;"
        )

        self._lbl_state.setText(status["operating_state"])

        interlock_open = status["interlock_open"]
        self._lbl_interlock.setText("OPEN (blocked)" if interlock_open else "Closed (OK)")

        fault = status["fault"]
        self._lbl_fault.setText(fault)
        self._btn_clear_fault.setEnabled(fault.strip().lower() != "no fault")

        self._lbl_autostart.setText("Yes" if status["autostart_enabled"] else "No")

        leds = status["leds"]
        led_names = {
            "power_on": "Power", "laser_on": "Laser On",
            "laser_lock": "Lock", "error": "Error",
        }
        self._lbl_leds.setText(
            "  ".join(f"{label}: {'ON' if leds[key] else 'off'}" for key, label in led_names.items())
        )

        self._lbl_hours.setText(f"{status['operating_hours']:.1f} h")

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_toggle(self, checked: bool) -> None:
        self._update_button_style(checked)
        self.enable_changed.emit(checked)

    def _update_button_style(self, on: bool) -> None:
        if on:
            self._btn_on.setText("Emission ON")
            self._btn_on.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        else:
            self._btn_on.setText("Emission OFF")
            self._btn_on.setStyleSheet("")

    def _on_trigger_source_changed(self, source: str) -> None:
        if not self._building:
            self.trigger_source_changed.emit(source)

    def _on_rate_changed(self, value: float) -> None:
        if not self._building:
            self.rate_changed.emit(value)
