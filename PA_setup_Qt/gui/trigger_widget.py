"""Laser trigger frequency modulation widget (NI-DAQ counter output)."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDoubleSpinBox, QLineEdit, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal


# Preset repetition rates common in PA microscopy
_PRESETS = [("100 Hz", 100.0), ("1 kHz", 1_000.0),
            ("10 kHz", 10_000.0), ("50 kHz", 50_000.0)]


class TriggerWidget(QGroupBox):
    """Controls the NI-DAQ counter output that triggers the laser.

    Emits start_requested(freq_hz, duty_cycle) and stop_requested().
    The parent window owns the TriggerController and acts on these signals.
    """

    start_requested = pyqtSignal(float, float)   # freq_hz, duty_cycle
    stop_requested  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Trigger (laser rep. rate)", parent)
        self._setup_ui()
        self.set_connected(False)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # --- Channel ---
        ch_row = QHBoxLayout()
        ch_row.addWidget(QLabel("Counter ch:"))
        self._inp_channel = QLineEdit("Dev1/ctr0")
        self._inp_channel.setFixedWidth(100)
        self._inp_channel.setToolTip(
            "NI-DAQ counter output channel.\n"
            "Output terminal depends on device (e.g. PFI12 on USB-6211).\n"
            "Check NI-MAX for your card."
        )
        ch_row.addWidget(self._inp_channel)
        ch_row.addStretch()
        layout.addLayout(ch_row)

        # --- Frequency ---
        layout.addWidget(QLabel("Repetition rate:"))
        freq_row = QHBoxLayout()
        self._spin_freq = QDoubleSpinBox()
        self._spin_freq.setRange(1.0, 100_000.0)
        self._spin_freq.setValue(1_000.0)
        self._spin_freq.setDecimals(1)
        self._spin_freq.setSuffix(" Hz")
        self._spin_freq.setFixedWidth(100)
        freq_row.addWidget(self._spin_freq)
        for label, hz in _PRESETS:
            btn = QPushButton(label)
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda _, v=hz: self._spin_freq.setValue(v))
            freq_row.addWidget(btn)
        layout.addLayout(freq_row)

        # --- Duty cycle ---
        duty_row = QHBoxLayout()
        duty_row.addWidget(QLabel("Duty cycle:"))
        self._spin_duty = QDoubleSpinBox()
        self._spin_duty.setRange(1.0, 50.0)
        self._spin_duty.setValue(5.0)
        self._spin_duty.setDecimals(1)
        self._spin_duty.setSuffix(" %")
        self._spin_duty.setFixedWidth(80)
        self._spin_duty.setToolTip(
            "Pulse width as % of period.\n"
            "At 1 kHz, 5 % = 50 µs pulse width.\n"
            "Keep short (≤10 %) for clean laser triggering."
        )
        duty_row.addWidget(self._spin_duty)
        duty_row.addStretch()
        layout.addLayout(duty_row)

        # --- Start / Stop ---
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("▶  Start")
        self._btn_start.setFixedHeight(30)
        self._btn_start.clicked.connect(self._on_start)

        self._btn_stop = QPushButton("■  Stop")
        self._btn_stop.setFixedHeight(30)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)

        self._lbl_running = QLabel("●")
        self._lbl_running.setStyleSheet("color: gray; font-size: 14px;")

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        btn_row.addWidget(self._lbl_running)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def channel(self) -> str:
        return self._inp_channel.text()

    def set_connected(self, connected: bool) -> None:
        self._inp_channel.setEnabled(not connected)  # lock channel while connected
        self._btn_start.setEnabled(connected)
        if not connected:
            self._btn_stop.setEnabled(False)
            self._set_running(False)

    def set_running(self, running: bool) -> None:
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._set_running(running)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        self.start_requested.emit(
            self._spin_freq.value(),
            self._spin_duty.value() / 100.0,
        )

    def _on_stop(self) -> None:
        self.stop_requested.emit()

    def _set_running(self, running: bool) -> None:
        color = "#4CAF50" if running else "gray"
        self._lbl_running.setStyleSheet(f"color: {color}; font-size: 14px;")
