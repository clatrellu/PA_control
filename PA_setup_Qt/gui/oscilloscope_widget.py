"""Oscilloscope display + acquisition control widget."""
from __future__ import annotations
import csv
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QDoubleSpinBox, QComboBox,
    QFileDialog, QSizePolicy,
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal, pyqtSlot

from pa_hardware.oscilloscope import RANGE_LABELS, CHANNEL_LABELS, COUPLING_LABELS

# Preset sample rates shown in the combo box
SAMPLE_RATES = {
    "125 MS/s": 125e6,
    "62.5 MS/s": 62.5e6,
    "31.25 MS/s": 31.25e6,
    "10 MS/s": 10e6,
    "1 MS/s": 1e6,
}


class _AcquisitionWorker(QObject):
    data_ready = pyqtSignal(object, object)   # time_us, voltage_mv (np.ndarray)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, scope, params: dict):
        super().__init__()
        self._scope = scope
        self._params = params
        self._running = False

    @pyqtSlot()
    def run(self):
        self._running = True
        while self._running:
            try:
                t, v = self._scope.capture_block(**self._params)
                self.data_ready.emit(t, v)
            except Exception as exc:
                self.error.emit(str(exc))
                break
        self.finished.emit()

    def stop(self):
        self._running = False


class OscilloscopeWidget(QWidget):
    """Waveform display and acquisition controls for a PicoScope 5000."""

    log_message = pyqtSignal(str)

    def __init__(self, scope, parent=None):
        super().__init__(parent)
        self._scope = scope
        self._last_time: np.ndarray | None = None
        self._last_voltage: np.ndarray | None = None
        self._thread: QThread | None = None
        self._worker: _AcquisitionWorker | None = None
        self._setup_ui()

    def set_scope(self, scope) -> None:
        """Replace the scope backend (e.g. when switching mock ↔ real)."""
        self._stop_acquisition()
        self._scope = scope

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(self._build_plot())
        layout.addWidget(self._build_controls())

    def _build_plot(self) -> pg.PlotWidget:
        pg.setConfigOptions(antialias=False, background="k", foreground="w")
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Time", units="µs")
        self._plot.setLabel("left", "Voltage", units="mV")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        pen = pg.mkPen(color=(0, 200, 255), width=1)
        self._curve = self._plot.plot(pen=pen)
        return self._plot

    def _build_controls(self) -> QGroupBox:
        group = QGroupBox("Acquisition")
        outer = QVBoxLayout(group)

        # --- Settings row ---
        settings = QHBoxLayout()

        settings.addWidget(QLabel("Sample rate:"))
        self._cb_rate = QComboBox()
        self._cb_rate.addItems(list(SAMPLE_RATES.keys()))
        self._cb_rate.setFixedWidth(110)
        settings.addWidget(self._cb_rate)

        settings.addWidget(QLabel("Duration:"))
        self._spin_duration = QDoubleSpinBox()
        self._spin_duration.setRange(0.01, 1000.0)
        self._spin_duration.setValue(1.0)
        self._spin_duration.setSingleStep(0.1)
        self._spin_duration.setSuffix(" ms")
        self._spin_duration.setFixedWidth(90)
        settings.addWidget(self._spin_duration)

        settings.addWidget(QLabel("Trigger:"))
        self._spin_trigger = QDoubleSpinBox()
        self._spin_trigger.setRange(-500.0, 500.0)
        self._spin_trigger.setValue(0.0)
        self._spin_trigger.setSuffix(" mV")
        self._spin_trigger.setFixedWidth(90)
        settings.addWidget(self._spin_trigger)

        settings.addWidget(QLabel("Ch:"))
        self._cb_channel = QComboBox()
        self._cb_channel.addItems(CHANNEL_LABELS)
        self._cb_channel.setFixedWidth(50)
        settings.addWidget(self._cb_channel)

        settings.addWidget(QLabel("Coupling:"))
        self._cb_coupling = QComboBox()
        self._cb_coupling.addItems(COUPLING_LABELS)
        self._cb_coupling.setCurrentText("DC")
        self._cb_coupling.setFixedWidth(55)
        settings.addWidget(self._cb_coupling)

        settings.addWidget(QLabel("Range:"))
        self._cb_range = QComboBox()
        self._cb_range.addItems(RANGE_LABELS)
        self._cb_range.setCurrentText("500 mV")
        self._cb_range.setFixedWidth(80)
        settings.addWidget(self._cb_range)

        settings.addStretch()
        outer.addLayout(settings)

        # --- Button row ---
        buttons = QHBoxLayout()
        self._btn_single = QPushButton("Single Capture")
        self._btn_single.setFixedHeight(30)
        self._btn_single.clicked.connect(self._start_single)

        self._btn_continuous = QPushButton("Continuous")
        self._btn_continuous.setFixedHeight(30)
        self._btn_continuous.setCheckable(True)
        self._btn_continuous.toggled.connect(self._on_continuous_toggled)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setFixedHeight(30)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_acquisition)

        self._btn_save = QPushButton("Save Trace…")
        self._btn_save.setFixedHeight(30)
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save_trace)

        for btn in (self._btn_single, self._btn_continuous,
                    self._btn_stop, self._btn_save):
            buttons.addWidget(btn)
        buttons.addStretch()
        outer.addLayout(buttons)

        return group

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def _get_acq_params(self) -> dict:
        self._scope.configure_channel(
            channel=self._cb_channel.currentText(),
            coupling=self._cb_coupling.currentText(),
            range_label=self._cb_range.currentText(),
        )
        return {
            "sample_rate_hz": SAMPLE_RATES[self._cb_rate.currentText()],
            "duration_ms": self._spin_duration.value(),
            "trigger_mv": self._spin_trigger.value(),
        }

    def _start_acquisition(self, continuous: bool) -> None:
        self._stop_acquisition()

        params = self._get_acq_params()
        self._worker = _AcquisitionWorker(self._scope, params)
        if not continuous:
            self._worker._running = False   # will run exactly once

        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.data_ready.connect(self._on_data)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_finished)

        if not continuous:
            # Single shot: make the worker stop after the first capture
            self._worker._running = True

            def _run_once():
                try:
                    t, v = self._scope.capture_block(**params)
                    self._worker.data_ready.emit(t, v)
                except Exception as exc:
                    self._worker.error.emit(str(exc))
                finally:
                    self._worker.finished.emit()

            self._thread.started.disconnect()
            self._thread.started.connect(_run_once)

        self._btn_stop.setEnabled(True)
        self._btn_single.setEnabled(False)
        self._thread.start()

    def _start_single(self) -> None:
        self._btn_continuous.blockSignals(True)
        self._btn_continuous.setChecked(False)
        self._btn_continuous.blockSignals(False)
        self._start_acquisition(continuous=False)

    def _on_continuous_toggled(self, checked: bool) -> None:
        if checked:
            self._btn_continuous.setStyleSheet(
                "background-color: #E65100; color: white; font-weight: bold;"
            )
            self._btn_continuous.setText("● Continuous")
            self._start_acquisition(continuous=True)
        else:
            self._btn_continuous.setStyleSheet("")
            self._btn_continuous.setText("Continuous")
            self._stop_acquisition()

    def _stop_acquisition(self) -> None:
        if self._worker:
            self._worker.stop()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(object, object)
    def _on_data(self, time_us: np.ndarray, voltage_mv: np.ndarray) -> None:
        self._last_time = time_us
        self._last_voltage = voltage_mv
        self._curve.setData(time_us, voltage_mv)
        self._btn_save.setEnabled(True)

    @pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        self.log_message.emit(f"[Scope] Error: {msg}")
        self._reset_buttons()

    @pyqtSlot()
    def _on_worker_finished(self) -> None:
        self._reset_buttons()

    def _reset_buttons(self) -> None:
        self._btn_stop.setEnabled(False)
        self._btn_single.setEnabled(True)
        self._btn_continuous.blockSignals(True)
        self._btn_continuous.setChecked(False)
        self._btn_continuous.setStyleSheet("")
        self._btn_continuous.setText("Continuous")
        self._btn_continuous.blockSignals(False)

    # ------------------------------------------------------------------
    # Save trace
    # ------------------------------------------------------------------

    def _save_trace(self) -> None:
        if self._last_time is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Trace", str(Path.home()), "CSV files (*.csv)"
        )
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_us", "voltage_mv"])
            writer.writerows(zip(self._last_time, self._last_voltage))
        self.log_message.emit(f"Trace saved → {path}")
