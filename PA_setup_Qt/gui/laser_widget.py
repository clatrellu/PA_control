"""Laser control panel widget."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDoubleSpinBox, QSlider, QComboBox, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal


class LaserWidget(QGroupBox):
    """Controls for the Cobolt laser: enable/disable, power setpoint, and modulation mode."""

    power_changed      = pyqtSignal(float)   # emitted when user changes setpoint
    enable_changed     = pyqtSignal(bool)    # emitted when user toggles emission
    mode_changed       = pyqtSignal(str)     # 'cw' or 'external'

    MAX_POWER_MW = 200.0

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
        self._btn_on.toggled.connect(self._on_toggle)
        row.addWidget(self._btn_on)
        layout.addLayout(row)

        # --- Power slider + spinbox ---
        layout.addWidget(QLabel("Power setpoint (mW):"))
        power_row = QHBoxLayout()
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, int(self.MAX_POWER_MW * 10))
        self._slider.setValue(0)
        self._slider.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._slider.valueChanged.connect(self._slider_to_spin)

        self._spin = QDoubleSpinBox()
        self._spin.setRange(0.0, self.MAX_POWER_MW)
        self._spin.setSingleStep(0.1)
        self._spin.setDecimals(1)
        self._spin.setSuffix(" mW")
        self._spin.setFixedWidth(90)
        self._spin.valueChanged.connect(self._spin_to_slider)

        power_row.addWidget(self._slider)
        power_row.addWidget(self._spin)
        layout.addLayout(power_row)

        # --- Actual power readback ---
        readback_row = QHBoxLayout()
        readback_row.addWidget(QLabel("Actual power:"))
        self._lbl_actual = QLabel("-- mW")
        self._lbl_actual.setAlignment(Qt.AlignmentFlag.AlignRight)
        readback_row.addWidget(self._lbl_actual)
        layout.addLayout(readback_row)

        # --- Modulation mode ---
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._cb_mode = QComboBox()
        self._cb_mode.addItems(["CW", "External trigger"])
        self._cb_mode.setToolTip(
            "CW: laser on continuously.\n"
            "External trigger: fires on each TTL pulse from the NI-DAQ counter output.\n"
            "Start the trigger before switching to External."
        )
        self._cb_mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._cb_mode)
        mode_row.addStretch()
        layout.addLayout(mode_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        self._btn_on.setEnabled(connected)
        self._slider.setEnabled(connected)
        self._spin.setEnabled(connected)
        self._cb_mode.setEnabled(connected)
        if not connected:
            self._btn_on.setChecked(False)
            self._lbl_actual.setText("-- mW")

    def update_actual_power(self, power_mw: float) -> None:
        self._lbl_actual.setText(f"{power_mw:.2f} mW")

    def update_emission(self, enabled: bool) -> None:
        self._btn_on.blockSignals(True)
        self._btn_on.setChecked(enabled)
        self._btn_on.blockSignals(False)
        self._update_button_style(enabled)

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

    def _slider_to_spin(self, value: int) -> None:
        mw = value / 10.0
        self._spin.blockSignals(True)
        self._spin.setValue(mw)
        self._spin.blockSignals(False)
        if not self._building:
            self.power_changed.emit(mw)

    def _spin_to_slider(self, value: float) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(int(value * 10))
        self._slider.blockSignals(False)
        if not self._building:
            self.power_changed.emit(value)

    def _on_mode_changed(self, index: int) -> None:
        if not self._building:
            self.mode_changed.emit("external" if index == 1 else "cw")
