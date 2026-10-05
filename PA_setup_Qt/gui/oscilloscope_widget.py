"""Oscilloscope display + acquisition control widget."""
from __future__ import annotations
import csv
import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QDoubleSpinBox, QComboBox,
    QFileDialog, QSizePolicy, QCheckBox,
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal, pyqtSlot

_AVERAGE_N = 50
_RATE_WINDOW_N = 20  # frames averaged over for the live acquisition-rate readout


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
                if len(v) == 0:
                    # No genuine trigger this cycle (Normal-mode semantics —
                    # see OscilloscopeController.capture_block); skip so the
                    # plot holds the last real trace instead of going blank.
                    continue
                self.data_ready.emit(t, v)
            except Exception as exc:
                self.error.emit(str(exc))
                break
        self.finished.emit()

    def stop(self):
        self._running = False

    @pyqtSlot()
    def run_once(self):
        """Single-shot capture, executed in the worker thread.

        A plain function connected to QThread.started (the previous
        approach) has no QObject to give Qt a thread affinity for, so it
        actually runs on whichever thread emits the signal — in this case
        the GUI thread itself, freezing the UI for up to 5s if a trigger is
        slow to arrive. A real method on this QObject (already moved to the
        worker thread) runs there instead, like run() already does.
        """
        try:
            t, v = self._scope.capture_block(**self._params)
            if len(v) == 0:
                self.error.emit("No trigger within timeout")
            else:
                self.data_ready.emit(t, v)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class OscilloscopeWidget(QWidget):
    """Waveform display and acquisition controls — adapts to any scope backend."""

    log_message = pyqtSignal(str)

    def __init__(self, scope, parent=None):
        super().__init__(parent)
        self._scope = scope
        self._last_time: np.ndarray | None = None
        self._last_voltage: np.ndarray | None = None
        # Rolling buffer of the last _AVERAGE_N raw voltage traces, purely
        # for the optional averaged display — never touches _last_time /
        # _last_voltage (Save Trace and everything else stay on raw data).
        self._avg_buffer: deque[np.ndarray] = deque(maxlen=_AVERAGE_N)
        # Timestamps of the last _RATE_WINDOW_N received frames, for the
        # live "Acq. rate" readout — measures actual achieved throughput,
        # not a theoretical one.
        self._frame_times: deque[float] = deque(maxlen=_RATE_WINDOW_N)
        self._thread: QThread | None = None
        self._worker: _AcquisitionWorker | None = None
        self._setup_ui()
        self._populate_combos()

    def set_scope(self, scope) -> None:
        """Replace the scope backend and refresh controls to match its capabilities."""
        self._stop_acquisition()
        self._scope = scope
        self._populate_combos()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(self._build_plot())
        layout.addWidget(self._build_controls())
        # Axis controls are built here (they wire up to self._plot) but not
        # added to this widget's own layout — the GUI is packed enough that
        # they're placed in MainWindow's left panel instead, where there's
        # more room. See the axis_controls attribute.
        self.axis_controls = self._build_axis_controls()

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
        self._cb_rate.setFixedWidth(110)
        self._cb_rate.currentTextChanged.connect(self._update_duration_tooltip)
        settings.addWidget(self._cb_rate)

        settings.addWidget(QLabel("Duration:"))
        self._spin_duration = QDoubleSpinBox()
        self._spin_duration.setRange(1.0, 100000.0)
        self._spin_duration.setDecimals(1)
        self._spin_duration.setValue(10.0)
        self._spin_duration.setSingleStep(1.0)
        self._spin_duration.setSuffix(" µs")
        self._spin_duration.setFixedWidth(90)
        self._spin_duration.valueChanged.connect(self._on_duration_changed)
        settings.addWidget(self._spin_duration)

        settings.addWidget(QLabel("Pre-trig:"))
        self._spin_pretrigger = QDoubleSpinBox()
        self._spin_pretrigger.setRange(0.0, self._spin_duration.value())
        self._spin_pretrigger.setDecimals(1)
        self._spin_pretrigger.setValue(0.0)
        self._spin_pretrigger.setSingleStep(1.0)
        self._spin_pretrigger.setSuffix(" µs")
        self._spin_pretrigger.setFixedWidth(90)
        self._spin_pretrigger.setToolTip(
            "How much of Duration is recorded *before* the trigger event,\n"
            "rather than after it. 0 (default) records only post-trigger,\n"
            "same as before. Capped at Duration — the rest is post-trigger."
        )
        settings.addWidget(self._spin_pretrigger)

        settings.addWidget(QLabel("Trigger:"))
        self._spin_trigger = QDoubleSpinBox()
        self._spin_trigger.setRange(-5000.0, 5000.0)
        self._spin_trigger.setValue(220.0)
        self._spin_trigger.setSuffix(" mV")
        self._spin_trigger.setFixedWidth(90)
        settings.addWidget(self._spin_trigger)

        settings.addStretch()
        outer.addLayout(settings)

        # --- Settings row 2: channel / trigger-source / range config ---
        settings2 = QHBoxLayout()

        settings2.addWidget(QLabel("Ch:"))
        self._cb_channel = QComboBox()
        self._cb_channel.setFixedWidth(55)
        settings2.addWidget(self._cb_channel)

        settings2.addWidget(QLabel("Trig src:"))
        self._cb_trig_source = QComboBox()
        self._cb_trig_source.setFixedWidth(90)
        self._cb_trig_source.setToolTip(
            "Which channel the trigger fires on.\n"
            "\"Same as Ch\" triggers on the recorded channel itself.\n"
            "Pick a different channel if the trigger source is wired in\n"
            "separately — e.g. a laser sync pulse on CH2 triggering a\n"
            "capture of the transducer signal on CH1."
        )
        self._cb_trig_source.currentTextChanged.connect(self._on_trig_source_changed)
        settings2.addWidget(self._cb_trig_source)

        settings2.addWidget(QLabel("Trig range:"))
        self._cb_trig_range = QComboBox()
        self._cb_trig_range.setFixedWidth(80)
        self._cb_trig_range.setToolTip(
            "Input range for the trigger channel, when it's different from\n"
            "the recorded channel. Set wide enough to avoid clipping the\n"
            "trigger source (e.g. a several-volt TTL sync pulse)."
        )
        settings2.addWidget(self._cb_trig_range)

        settings2.addWidget(QLabel("Trig edge:"))
        self._cb_trig_edge = QComboBox()
        self._cb_trig_edge.addItems(["Rising", "Falling"])
        self._cb_trig_edge.setFixedWidth(70)
        self._cb_trig_edge.setToolTip(
            "Which edge of the trigger source fires the capture.\n"
            "Use \"Falling\" for sources that dip low on the actual event\n"
            "and recover afterward (e.g. some lasers' Signal Out) — \"Rising\"\n"
            "there would trigger on the recovery, not the event itself."
        )
        settings2.addWidget(self._cb_trig_edge)

        settings2.addWidget(QLabel("Coupling:"))
        self._cb_coupling = QComboBox()
        self._cb_coupling.setFixedWidth(55)
        settings2.addWidget(self._cb_coupling)

        settings2.addWidget(QLabel("Range:"))
        self._cb_range = QComboBox()
        self._cb_range.setFixedWidth(80)
        settings2.addWidget(self._cb_range)

        settings2.addStretch()
        outer.addLayout(settings2)

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

        self._btn_average = QPushButton(f"Average ({_AVERAGE_N})")
        self._btn_average.setFixedHeight(30)
        self._btn_average.setCheckable(True)
        self._btn_average.setToolTip(
            f"Display the running average of the last {_AVERAGE_N} captures\n"
            "instead of the raw single-shot trace. Display only — acquisition,\n"
            "Save Trace, and everything else still use the raw data."
        )
        self._btn_average.toggled.connect(self._on_average_toggled)

        self._btn_save_average = QPushButton("Save Average…")
        self._btn_save_average.setFixedHeight(30)
        self._btn_save_average.setEnabled(False)
        self._btn_save_average.setToolTip(
            f"Save the running average of the last {_AVERAGE_N} captures to CSV,\n"
            "regardless of whether the Average display is currently toggled on."
        )
        self._btn_save_average.clicked.connect(self._save_average_trace)

        for btn in (self._btn_single, self._btn_continuous,
                    self._btn_stop, self._btn_save, self._btn_average,
                    self._btn_save_average):
            buttons.addWidget(btn)
        buttons.addStretch()

        buttons.addWidget(QLabel("Acq. rate:"))
        self._lbl_acq_rate = QLabel("-- Hz")
        self._lbl_acq_rate.setStyleSheet("font-weight: bold;")
        self._lbl_acq_rate.setToolTip(
            "Actual achieved capture rate, measured from real inter-frame\n"
            f"timing over the last {_RATE_WINDOW_N} frames received — not a\n"
            "theoretical figure. Continuous mode only; a genuine miss (no\n"
            "trigger that cycle) doesn't count as a frame, so this reflects\n"
            "real throughput, not the underlying trigger source's rate."
        )
        buttons.addWidget(self._lbl_acq_rate)

        outer.addLayout(buttons)

        return group

    def _build_axis_controls(self) -> QGroupBox:
        group = QGroupBox("Axes")
        outer = QVBoxLayout(group)

        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X:"))
        self._chk_x_auto = QCheckBox("Auto")
        self._chk_x_auto.setChecked(True)
        self._chk_x_auto.toggled.connect(self._on_x_auto_toggled)
        x_row.addWidget(self._chk_x_auto)

        self._spin_x_min = QDoubleSpinBox()
        self._spin_x_min.setRange(-1e6, 1e6)
        self._spin_x_min.setDecimals(2)
        self._spin_x_min.setValue(0.0)
        self._spin_x_min.setSuffix(" µs")
        self._spin_x_min.setFixedWidth(100)
        self._spin_x_min.setEnabled(False)
        self._spin_x_min.valueChanged.connect(self._on_x_range_changed)
        x_row.addWidget(self._spin_x_min)

        x_row.addWidget(QLabel("to"))

        self._spin_x_max = QDoubleSpinBox()
        self._spin_x_max.setRange(-1e6, 1e6)
        self._spin_x_max.setDecimals(2)
        self._spin_x_max.setValue(1000.0)
        self._spin_x_max.setSuffix(" µs")
        self._spin_x_max.setFixedWidth(100)
        self._spin_x_max.setEnabled(False)
        self._spin_x_max.valueChanged.connect(self._on_x_range_changed)
        x_row.addWidget(self._spin_x_max)

        x_row.addStretch()
        outer.addLayout(x_row)

        y_row = QHBoxLayout()
        y_row.addWidget(QLabel("Y:"))
        self._chk_y_auto = QCheckBox("Auto")
        self._chk_y_auto.setChecked(True)
        self._chk_y_auto.toggled.connect(self._on_y_auto_toggled)
        y_row.addWidget(self._chk_y_auto)

        self._spin_y_min = QDoubleSpinBox()
        self._spin_y_min.setRange(-1e5, 1e5)
        self._spin_y_min.setDecimals(1)
        self._spin_y_min.setValue(-1000.0)
        self._spin_y_min.setSuffix(" mV")
        self._spin_y_min.setFixedWidth(100)
        self._spin_y_min.setEnabled(False)
        self._spin_y_min.valueChanged.connect(self._on_y_range_changed)
        y_row.addWidget(self._spin_y_min)

        y_row.addWidget(QLabel("to"))

        self._spin_y_max = QDoubleSpinBox()
        self._spin_y_max.setRange(-1e5, 1e5)
        self._spin_y_max.setDecimals(1)
        self._spin_y_max.setValue(1000.0)
        self._spin_y_max.setSuffix(" mV")
        self._spin_y_max.setFixedWidth(100)
        self._spin_y_max.setEnabled(False)
        self._spin_y_max.valueChanged.connect(self._on_y_range_changed)
        y_row.addWidget(self._spin_y_max)

        y_row.addStretch()
        outer.addLayout(y_row)

        return group

    def _on_x_auto_toggled(self, checked: bool) -> None:
        self._spin_x_min.setEnabled(not checked)
        self._spin_x_max.setEnabled(not checked)
        if checked:
            self._plot.enableAutoRange(axis="x", enable=True)
        else:
            self._plot.setXRange(
                self._spin_x_min.value(), self._spin_x_max.value(), padding=0
            )

    def _on_x_range_changed(self, _value: float) -> None:
        if not self._chk_x_auto.isChecked():
            self._plot.setXRange(
                self._spin_x_min.value(), self._spin_x_max.value(), padding=0
            )

    def _on_y_auto_toggled(self, checked: bool) -> None:
        self._spin_y_min.setEnabled(not checked)
        self._spin_y_max.setEnabled(not checked)
        if checked:
            self._plot.enableAutoRange(axis="y", enable=True)
        else:
            self._plot.setYRange(
                self._spin_y_min.value(), self._spin_y_max.value(), padding=0
            )

    def _on_y_range_changed(self, _value: float) -> None:
        if not self._chk_y_auto.isChecked():
            self._plot.setYRange(
                self._spin_y_min.value(), self._spin_y_max.value(), padding=0
            )

    def _populate_combos(self) -> None:
        """Fill combo boxes with options exposed by the current scope class."""
        cls = type(self._scope)

        self._cb_rate.clear()
        self._cb_rate.addItems(list(cls.SAMPLE_RATES.keys()))

        self._cb_channel.clear()
        self._cb_channel.addItems(cls.CHANNEL_LABELS)

        self._cb_coupling.clear()
        self._cb_coupling.addItems(cls.COUPLING_LABELS)
        if "DC" in cls.COUPLING_LABELS:
            self._cb_coupling.setCurrentText("DC")

        self._cb_range.clear()
        self._cb_range.addItems(cls.RANGE_LABELS)
        if "1 V" in cls.RANGE_LABELS:
            self._cb_range.setCurrentText("1 V")

        self._cb_trig_source.clear()
        self._cb_trig_source.addItems(["Same as Ch"] + list(cls.CHANNEL_LABELS))

        self._cb_trig_range.clear()
        self._cb_trig_range.addItems(cls.RANGE_LABELS)
        if "20 V" in cls.RANGE_LABELS:
            self._cb_trig_range.setCurrentText("20 V")
        elif cls.RANGE_LABELS:
            self._cb_trig_range.setCurrentIndex(len(cls.RANGE_LABELS) - 1)
        self._cb_trig_range.setEnabled(False)

        self._update_duration_tooltip()

    def _on_trig_source_changed(self, text: str) -> None:
        self._cb_trig_range.setEnabled(text != "Same as Ch")

    def _on_duration_changed(self, duration_us: float) -> None:
        """Keep Pre-trig capped at the current Duration (it can't exceed it)."""
        self._spin_pretrigger.setMaximum(duration_us)

    def _update_duration_tooltip(self) -> None:
        """Explain the duration field's hardware-buffer clamp for the current rate."""
        cls = type(self._scope)
        max_samples = getattr(cls, "MAX_SAMPLES", None)
        rate_label = self._cb_rate.currentText()

        if not max_samples or not rate_label:
            self._spin_duration.setToolTip(
                "Total length of the captured window (pre-trigger + post-trigger).\n"
                "The scope's onboard buffer holds a fixed number of samples, so\n"
                "past a certain point raising the duration has no effect — it\n"
                "silently clamps to whatever that buffer holds at the current\n"
                "sample rate. Lower the sample rate for a longer capture window."
            )
            return

        rate_hz = cls.SAMPLE_RATES[rate_label]
        max_us = max_samples / rate_hz * 1e6
        self._spin_duration.setToolTip(
            "Total length of the captured window (pre-trigger + post-trigger).\n"
            f"The scope's buffer holds {max_samples:,} samples, so at {rate_label}\n"
            f"the longest possible capture is ~{max_us:.3g} µs — requesting more\n"
            "just clamps to that limit. Lower the sample rate for a longer window."
        )

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def _get_acq_params(self) -> dict:
        cls = type(self._scope)
        self._scope.configure_channel(
            channel=self._cb_channel.currentText(),
            coupling=self._cb_coupling.currentText(),
            range_label=self._cb_range.currentText(),
        )
        trig_source = self._cb_trig_source.currentText()
        self._scope.configure_trigger_channel(
            channel=None if trig_source == "Same as Ch" else trig_source,
            range_label=self._cb_trig_range.currentText(),
            edge=self._cb_trig_edge.currentText().lower(),
        )
        return {
            "sample_rate_hz": cls.SAMPLE_RATES[self._cb_rate.currentText()],
            # The scope API is in milliseconds; the UI displays microseconds
            # for finer control at short durations, so convert at this
            # boundary — the only place unit conversion should happen.
            "duration_ms": self._spin_duration.value() / 1e3,
            "trigger_mv": self._spin_trigger.value(),
            "pretrigger_ms": self._spin_pretrigger.value() / 1e3,
        }

    def get_acq_params(self) -> dict:
        """Public accessor for external callers (e.g. the Scan tab) that need
        the scope's current acquisition settings without duplicating the
        UI's own config logic. Also applies the channel/trigger config to
        the scope, same as starting a capture from this widget would."""
        return self._get_acq_params()

    def set_scan_active(self, active: bool) -> None:
        """Lock out manual capture while an external scan owns the scope."""
        if active:
            self._stop_acquisition()
        self._btn_single.setEnabled(not active)
        self._btn_continuous.setEnabled(not active)

    def _start_acquisition(self, continuous: bool) -> None:
        self._stop_acquisition()
        self._avg_buffer.clear()
        self._frame_times.clear()
        self._lbl_acq_rate.setText("-- Hz")

        params = self._get_acq_params()
        self._worker = _AcquisitionWorker(self._scope, params)

        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        if continuous:
            self._thread.started.connect(self._worker.run)
        else:
            self._thread.started.connect(self._worker.run_once)
        self._worker.data_ready.connect(self._on_data)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)

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
            if not self._thread.wait(2000):
                # Still running — worker.stop() only takes effect on the
                # *next* loop iteration, but a single capture_block() call
                # can block for several seconds waiting on a real trigger
                # edge (see OscilloscopeController.capture_block). Wait as
                # long as it actually takes rather than dropping the QThread
                # reference while its underlying OS thread is still alive —
                # that mismatch is what "QThread: Destroyed while thread is
                # still running" means, and it can crash the app.
                self._thread.wait()
        self._thread = None
        self._worker = None

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(object, object)
    def _on_data(self, time_us: np.ndarray, voltage_mv: np.ndarray) -> None:
        self._last_time = time_us
        self._last_voltage = voltage_mv

        # A shape change (duration/sample-rate change mid-stream) makes the
        # buffered traces incomparable — start the rolling average over.
        if self._avg_buffer and len(self._avg_buffer[-1]) != len(voltage_mv):
            self._avg_buffer.clear()
        self._avg_buffer.append(voltage_mv)

        self._curve.setData(time_us, self._display_voltage())
        self._btn_save.setEnabled(True)
        self._btn_save_average.setEnabled(True)

        self._frame_times.append(time.monotonic())
        if len(self._frame_times) >= 2:
            elapsed = self._frame_times[-1] - self._frame_times[0]
            if elapsed > 0:
                rate_hz = (len(self._frame_times) - 1) / elapsed
                self._lbl_acq_rate.setText(f"{rate_hz:.1f} Hz")

    def _display_voltage(self) -> np.ndarray | None:
        """What the plot should show for the latest data: the running
        average of the buffered traces if Average is on, else the raw
        latest trace. Display-only — never affects _last_voltage/Save Trace.
        """
        if self._btn_average.isChecked() and self._avg_buffer:
            return np.mean(self._avg_buffer, axis=0)
        return self._last_voltage

    def _on_average_toggled(self, _checked: bool) -> None:
        """Re-render immediately with whatever's already buffered, rather
        than waiting for the next capture to reflect the new toggle state."""
        if self._last_time is not None:
            self._curve.setData(self._last_time, self._display_voltage())

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
        self._lbl_acq_rate.setText("-- Hz")

    # ------------------------------------------------------------------
    # Save trace
    # ------------------------------------------------------------------

    _DEFAULT_SAVE_DIR = Path("/home/xray/Documents/setup/traces")

    def _save_trace(self) -> None:
        if self._last_time is None:
            return
        self._DEFAULT_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Trace", str(self._DEFAULT_SAVE_DIR), "CSV files (*.csv)"
        )
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_us", "voltage_mv"])
            writer.writerows(zip(self._last_time, self._last_voltage))
        self.log_message.emit(f"Trace saved → {path}")

    def _save_average_trace(self) -> None:
        if self._last_time is None or not self._avg_buffer:
            return
        self._DEFAULT_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Average Trace", str(self._DEFAULT_SAVE_DIR),
            "CSV files (*.csv)"
        )
        if not path:
            return
        avg_voltage = np.mean(self._avg_buffer, axis=0)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_us", "voltage_mv"])
            writer.writerows(zip(self._last_time, avg_voltage))
        self.log_message.emit(
            f"Average trace ({len(self._avg_buffer)} captures) saved → {path}"
        )
