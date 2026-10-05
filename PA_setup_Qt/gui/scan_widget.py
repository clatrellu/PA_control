"""Position-scanning acquisition tab.

Steps a Zaber T-LLS105 translation stage between positions, averaging N
triggered oscilloscope captures at each stop and saving the result to CSV,
before moving on to the next position.
"""
from __future__ import annotations
import csv
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QGridLayout,
    QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QLineEdit,
    QCheckBox, QProgressBar, QFileDialog,
)
from PyQt6.QtCore import QThread, QObject, QTimer, pyqtSignal, pyqtSlot

from pa_hardware.stage import MAX_SPEED_MM_S, MM_S_PER_SPEED_UNIT

_DEFAULT_SCAN_DIR = Path("/home/xray/Documents/setup/traces/scans")
# A position never advances on a timeout — it waits for genuine triggered
# captures for as long as it takes (only a user Stop breaks out early), so
# a scan can never contain a "position" averaged from data the laser didn't
# actually produce. This just throttles the "still waiting" log line so a
# stalled laser is visible without spamming the log every capture attempt.
_WAIT_LOG_INTERVAL_S = 5.0
_MOVE_TIMEOUT_S = 30.0


class _StagePositionWorker(QObject):
    """Polls stage.get_position_mm() on a background thread — each poll is a
    blocking serial round-trip, so running it on the UI thread stalls the
    whole GUI whenever the stage is slow to answer (same issue the laser
    status polling in MainWindow already works around)."""

    position_ready = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, stage, poll_interval_s: float = 0.5):
        super().__init__()
        self._stage = stage
        self._interval = poll_interval_s
        self._running = True

    @pyqtSlot()
    def run(self) -> None:
        while self._running:
            if self._stage.is_connected:
                try:
                    pos = self._stage.get_position_mm()
                    self.position_ready.emit(pos)
                except Exception:
                    pass  # transient serial hiccup — try again next poll
            elapsed = 0.0
            while self._running and elapsed < self._interval:
                time.sleep(0.1)
                elapsed += 0.1
        self.finished.emit()

    def stop(self) -> None:
        self._running = False


class _ScanWorker(QObject):
    progress = pyqtSignal(int, int, str)       # done, total, status text
    position_saved = pyqtSignal(int, float, str)  # index, pos_mm, path
    log = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self, stage, scope, positions_mm: list[float], acq_params: dict,
        n_average: int, out_dir: Path, settle_s: float, return_to_start: bool,
        move_speed_mm_s: float, return_speed_mm_s: float,
    ):
        super().__init__()
        self._stage = stage
        self._scope = scope
        self._positions = positions_mm
        self._acq_params = acq_params
        self._n_average = n_average
        self._out_dir = out_dir
        self._settle_s = settle_s
        self._return_to_start = return_to_start
        self._move_speed = move_speed_mm_s
        self._return_speed = return_speed_mm_s
        self._running = False

    def stop(self) -> None:
        self._running = False

    @pyqtSlot()
    def run(self) -> None:
        self._running = True
        total = len(self._positions)
        original_speed = None
        try:
            # Target Speed is a persistent device setting, so remember it and
            # restore it in the finally below — the scan must not leave the
            # stage permanently slower for later manual use.
            try:
                original_speed = self._stage.get_target_speed_raw()
                self._stage.set_target_speed_mm_s(self._move_speed)
                self.log.emit(
                    f"Stage speed was {original_speed * MM_S_PER_SPEED_UNIT:.3f} "
                    f"mm/s (raw {original_speed}); scanning at "
                    f"{self._move_speed:.3f} mm/s, will restore afterward."
                )
            except Exception as exc:
                self.log.emit(
                    f"Could not set move speed ({exc}) — using the stage's "
                    "current speed."
                )
            for i, pos_mm in enumerate(self._positions):
                if not self._running:
                    break
                self.progress.emit(i, total, f"Moving to {pos_mm:+.4f} mm…")
                self._stage.move_absolute_mm(pos_mm)
                idle = self._wait_until_idle()
                if not self._running:
                    break
                if not idle:
                    self.log.emit(
                        f"Stage move to {pos_mm:+.4f} mm timed out — skipping."
                    )
                    continue
                if self._settle_s > 0:
                    time.sleep(self._settle_s)

                traces: list[np.ndarray] = []
                last_time = None
                last_wait_log = time.monotonic()
                while self._running and len(traces) < self._n_average:
                    t, v = self._scope.capture_block(**self._acq_params)
                    if len(v) == 0:
                        # No genuine trigger this cycle — the laser hasn't
                        # fired (or missed threshold). Never counts as an
                        # acquisition; just keep waiting rather than moving
                        # on, so a position can't be averaged from data the
                        # laser didn't actually produce.
                        now = time.monotonic()
                        if now - last_wait_log >= _WAIT_LOG_INTERVAL_S:
                            self.log.emit(
                                f"Position {i + 1}/{total} ({pos_mm:+.4f} mm): "
                                f"waiting for laser trigger… "
                                f"({len(traces)}/{self._n_average} so far)"
                            )
                            last_wait_log = now
                        continue
                    last_time = t
                    traces.append(v)
                    self.progress.emit(
                        i, total,
                        f"Position {i + 1}/{total} ({pos_mm:+.4f} mm) — "
                        f"{len(traces)}/{self._n_average} traces"
                    )

                if not self._running:
                    # Stopped mid-position: discard the partial average
                    # rather than save data the laser didn't fully produce.
                    break

                avg_voltage = np.mean(traces, axis=0)
                path = self._out_dir / f"pos{i:03d}_{pos_mm:+.4f}mm.csv"
                with open(path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["time_us", "voltage_mv"])
                    writer.writerows(zip(last_time, avg_voltage))
                self.position_saved.emit(i, pos_mm, str(path))
                self.log.emit(
                    f"Saved {len(traces)}/{self._n_average}-avg trace "
                    f"({pos_mm:+.4f} mm) → {path.name}"
                )

            if self._running and self._return_to_start and self._positions:
                self.progress.emit(total, total, "Returning to start position…")
                self._move_to_return_start()

            self.progress.emit(
                total, total, "Scan complete." if self._running else "Scan stopped."
            )
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if original_speed is not None:
                try:
                    self._stage.set_target_speed_raw(original_speed)
                except Exception as exc:
                    self.log.emit(f"Could not restore original stage speed: {exc}")
            self.finished.emit()

    def _move_to_return_start(self) -> None:
        try:
            self._stage.set_target_speed_mm_s(self._return_speed)
        except Exception as exc:
            self.log.emit(
                f"Could not set return speed ({exc}) — returning at move speed."
            )
        self._stage.move_absolute_mm(self._positions[0])
        self._wait_until_idle()

    def _wait_until_idle(self, timeout_s: float = _MOVE_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout_s
        while self._running and time.monotonic() < deadline:
            if not self._stage.is_busy():
                return True
            time.sleep(0.05)
        return False


class ScanWidget(QWidget):
    """Scan tab: stage positioning + per-position averaged acquisition."""

    log_message = pyqtSignal(str)

    def __init__(self, stage, scope, oscilloscope_widget, parent=None):
        super().__init__(parent)
        self._stage = stage
        self._scope = scope
        self._osc_widget = oscilloscope_widget
        self._thread: QThread | None = None
        self._worker: _ScanWorker | None = None
        self._pos_thread: QThread | None = None
        self._pos_worker: _StagePositionWorker | None = None
        self._setup_ui()

        # Cheap UI-thread check (a plain attribute read, no I/O) to blank
        # the readout when disconnected; the actual serial polling that
        # updates it while connected runs on a background thread (below).
        self._pos_idle_timer = QTimer(self)
        self._pos_idle_timer.setInterval(500)
        self._pos_idle_timer.timeout.connect(self._check_stage_connected)
        self._pos_idle_timer.start()

        self._start_position_polling()

    def _start_position_polling(self) -> None:
        self._pos_thread = QThread()
        self._pos_worker = _StagePositionWorker(self._stage)
        self._pos_worker.moveToThread(self._pos_thread)
        self._pos_thread.started.connect(self._pos_worker.run)
        self._pos_worker.position_ready.connect(self._on_position_ready)
        self._pos_worker.finished.connect(self._pos_thread.quit)
        self._pos_thread.start()

    def shutdown(self) -> None:
        """Stop background polling — call this before the app closes."""
        if self._pos_worker is not None:
            self._pos_worker.stop()
        if self._pos_thread is not None:
            self._pos_thread.quit()
            if not self._pos_thread.wait(2000):
                self._pos_thread.wait()
        self._pos_thread = None
        self._pos_worker = None

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(self._build_stage_group())
        self._grp_positions = self._build_positions_group()
        layout.addWidget(self._grp_positions)
        self._grp_acquisition = self._build_acquisition_group()
        layout.addWidget(self._grp_acquisition)
        layout.addWidget(self._build_run_group())
        layout.addStretch()

    def _build_stage_group(self) -> QGroupBox:
        group = QGroupBox("Stage")
        row = QHBoxLayout(group)
        row.addWidget(QLabel("Position:"))
        self._lbl_position = QLabel("-- mm")
        self._lbl_position.setStyleSheet("font-weight: bold;")
        row.addWidget(self._lbl_position)
        row.addStretch()
        self._btn_home = QPushButton("Home")
        self._btn_home.clicked.connect(self._on_home)
        row.addWidget(self._btn_home)
        return group

    def _build_positions_group(self) -> QGroupBox:
        group = QGroupBox("Scan Positions")
        grid = QGridLayout(group)

        grid.addWidget(QLabel("Start:"), 0, 0)
        self._spin_start = QDoubleSpinBox()
        self._spin_start.setRange(-1000.0, 1000.0)
        self._spin_start.setDecimals(4)
        self._spin_start.setSuffix(" mm")
        self._spin_start.setFixedWidth(110)
        grid.addWidget(self._spin_start, 0, 1)
        btn_start_here = QPushButton("Set = current")
        btn_start_here.clicked.connect(lambda: self._set_from_current(self._spin_start))
        grid.addWidget(btn_start_here, 0, 2)

        grid.addWidget(QLabel("Stop:"), 1, 0)
        self._spin_stop = QDoubleSpinBox()
        self._spin_stop.setRange(-1000.0, 1000.0)
        self._spin_stop.setDecimals(4)
        self._spin_stop.setValue(10.0)
        self._spin_stop.setSuffix(" mm")
        self._spin_stop.setFixedWidth(110)
        grid.addWidget(self._spin_stop, 1, 1)
        btn_stop_here = QPushButton("Set = current")
        btn_stop_here.clicked.connect(lambda: self._set_from_current(self._spin_stop))
        grid.addWidget(btn_stop_here, 1, 2)

        grid.addWidget(QLabel("Step:"), 2, 0)
        self._spin_step = QDoubleSpinBox()
        self._spin_step.setRange(0.0001, 1000.0)
        self._spin_step.setDecimals(4)
        self._spin_step.setValue(1.0)
        self._spin_step.setSuffix(" mm")
        self._spin_step.setFixedWidth(110)
        grid.addWidget(self._spin_step, 2, 1)

        self._lbl_n_positions = QLabel()
        grid.addWidget(self._lbl_n_positions, 2, 2)

        for spin in (self._spin_start, self._spin_stop, self._spin_step):
            spin.valueChanged.connect(self._update_position_count)
        self._update_position_count()

        return group

    def _build_acquisition_group(self) -> QGroupBox:
        group = QGroupBox("Acquisition per Position")
        grid = QGridLayout(group)

        grid.addWidget(QLabel("Averages:"), 0, 0)
        self._spin_average = QSpinBox()
        self._spin_average.setRange(1, 10000)
        self._spin_average.setValue(50)
        self._spin_average.setFixedWidth(90)
        grid.addWidget(self._spin_average, 0, 1)

        grid.addWidget(QLabel("Settle time:"), 0, 2)
        self._spin_settle = QDoubleSpinBox()
        self._spin_settle.setRange(0.0, 60.0)
        self._spin_settle.setDecimals(2)
        self._spin_settle.setValue(0.2)
        self._spin_settle.setSuffix(" s")
        self._spin_settle.setFixedWidth(90)
        self._spin_settle.setToolTip(
            "Extra pause after the stage reports idle, before capturing —\n"
            "lets mechanical vibration from the move settle out."
        )
        grid.addWidget(self._spin_settle, 0, 3)

        grid.addWidget(QLabel("Output dir:"), 1, 0)
        self._edit_out_dir = QLineEdit(str(_DEFAULT_SCAN_DIR))
        grid.addWidget(self._edit_out_dir, 1, 1, 1, 2)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._on_browse_dir)
        grid.addWidget(btn_browse, 1, 3)

        grid.addWidget(QLabel("Scan name:"), 2, 0)
        self._edit_scan_name = QLineEdit("scan")
        self._edit_scan_name.setToolTip(
            "Each run is saved into its own timestamped subfolder named\n"
            "from this, so repeated scans never overwrite each other."
        )
        grid.addWidget(self._edit_scan_name, 2, 1)

        grid.addWidget(QLabel("Move speed:"), 3, 0)
        self._spin_move_speed = self._make_speed_spin(
            "Stage Target Speed used for the moves between scan positions.\n"
            "Lower it if you suspect lost steps (open-loop stepper, no\n"
            "encoder). The stage's original speed is restored when the\n"
            "scan ends. Default is a judgment call, not a measured value."
        )
        grid.addWidget(self._spin_move_speed, 3, 1)

        self._spin_return_speed = self._make_speed_spin(
            "Stage Target Speed for the return-to-start move. The stage's\n"
            "original speed is restored when the scan ends."
        )
        self._chk_return = QCheckBox("Return to start:")
        self._chk_return.setChecked(True)
        self._chk_return.toggled.connect(self._spin_return_speed.setEnabled)
        grid.addWidget(self._chk_return, 3, 2)
        grid.addWidget(self._spin_return_speed, 3, 3)

        return group

    @staticmethod
    def _make_speed_spin(tooltip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.01, MAX_SPEED_MM_S)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setValue(1.0)
        spin.setSuffix(" mm/s")
        spin.setFixedWidth(100)
        spin.setToolTip(tooltip)
        return spin

    def _build_run_group(self) -> QGroupBox:
        group = QGroupBox("Run")
        layout = QVBoxLayout(group)

        btn_row = QHBoxLayout()
        self._btn_start_scan = QPushButton("Start Scan")
        self._btn_start_scan.setFixedHeight(32)
        self._btn_start_scan.clicked.connect(self._on_start_scan)
        btn_row.addWidget(self._btn_start_scan)

        self._btn_stop_scan = QPushButton("Stop")
        self._btn_stop_scan.setFixedHeight(32)
        self._btn_stop_scan.setEnabled(False)
        self._btn_stop_scan.clicked.connect(self._on_stop_scan)
        btn_row.addWidget(self._btn_stop_scan)
        layout.addLayout(btn_row)

        self._progress = QProgressBar()
        layout.addWidget(self._progress)

        self._lbl_status = QLabel("Idle.")
        self._lbl_status.setWordWrap(True)
        layout.addWidget(self._lbl_status)

        return group

    # ------------------------------------------------------------------
    # Stage helpers
    # ------------------------------------------------------------------

    def _check_stage_connected(self) -> None:
        if not self._stage.is_connected:
            self._lbl_position.setText("-- mm")

    @pyqtSlot(float)
    def _on_position_ready(self, pos_mm: float) -> None:
        self._lbl_position.setText(f"{pos_mm:.4f} mm")

    def _set_from_current(self, spin: QDoubleSpinBox) -> None:
        if not self._stage.is_connected:
            self.log_message.emit("[Scan] Stage not connected.")
            return
        try:
            spin.setValue(self._stage.get_position_mm())
        except Exception as e:
            self.log_message.emit(f"[Scan] Could not read position: {e}")

    def _on_home(self) -> None:
        if not self._stage.is_connected:
            self.log_message.emit("[Scan] Stage not connected.")
            return
        try:
            self._stage.home()
            self.log_message.emit("[Scan] Stage homing…")
        except Exception as e:
            self.log_message.emit(f"[Scan] Home error: {e}")

    def _update_position_count(self, *_args) -> None:
        self._lbl_n_positions.setText(f"{len(self._positions_mm())} position(s)")

    def _positions_mm(self) -> list[float]:
        start = self._spin_start.value()
        stop = self._spin_stop.value()
        step = abs(self._spin_step.value())
        if step == 0:
            return [start]
        n = int(round(abs(stop - start) / step)) + 1
        sign = 1.0 if stop >= start else -1.0
        return [round(start + sign * step * i, 6) for i in range(n)]

    def _on_browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Scan Output Directory", self._edit_out_dir.text()
        )
        if path:
            self._edit_out_dir.setText(path)

    # ------------------------------------------------------------------
    # Scan control
    # ------------------------------------------------------------------

    def _on_start_scan(self) -> None:
        if not self._stage.is_connected:
            self.log_message.emit("[Scan] Cannot start — stage not connected.")
            return
        # Continuous acquisition on the Oscilloscope tab runs in a child
        # process holding the scope connection; get it back first.
        self._osc_widget.stop_acquisition()
        if not self._scope.is_connected:
            self.log_message.emit("[Scan] Cannot start — scope not connected.")
            return

        positions = self._positions_mm()
        acq_params = self._osc_widget.get_acq_params()
        if acq_params.get("trigger_mv", 0.0) == 0.0:
            self.log_message.emit(
                "[Scan] Cannot start — Trigger is set to 0 mV on the "
                "Oscilloscope tab, which forces a capture immediately "
                "instead of waiting for a real laser pulse. Set a genuine "
                "trigger threshold there first."
            )
            return
        base_dir = Path(self._edit_out_dir.text().strip() or str(_DEFAULT_SCAN_DIR))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = self._edit_scan_name.text().strip() or "scan"
        out_dir = base_dir / f"{name}_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        self._osc_widget.set_scan_active(True)
        self._grp_positions.setEnabled(False)
        self._grp_acquisition.setEnabled(False)
        self._btn_home.setEnabled(False)
        self._progress.setMaximum(len(positions))
        self._progress.setValue(0)
        self._lbl_status.setText("Starting scan…")
        self.log_message.emit(
            f"[Scan] Starting {len(positions)}-position scan → {out_dir}"
        )

        self._worker = _ScanWorker(
            self._stage, self._scope, positions, acq_params,
            self._spin_average.value(), out_dir,
            self._spin_settle.value(), self._chk_return.isChecked(),
            self._spin_move_speed.value(), self._spin_return_speed.value(),
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self.log_message)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._btn_start_scan.setEnabled(False)
        self._btn_stop_scan.setEnabled(True)
        self._thread.start()

    def _on_stop_scan(self) -> None:
        if self._worker:
            self._worker.stop()
        try:
            self._stage.stop()
        except Exception:
            pass
        self.log_message.emit("[Scan] Stop requested…")

    @pyqtSlot(int, int, str)
    def _on_progress(self, done: int, total: int, text: str) -> None:
        self._progress.setMaximum(max(total, 1))
        self._progress.setValue(done)
        self._lbl_status.setText(text)

    @pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        self.log_message.emit(f"[Scan] Error: {msg}")

    @pyqtSlot()
    def _on_finished(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
        self._osc_widget.set_scan_active(False)
        self._grp_positions.setEnabled(True)
        self._grp_acquisition.setEnabled(True)
        self._btn_home.setEnabled(True)
        self._btn_start_scan.setEnabled(True)
        self._btn_stop_scan.setEnabled(False)
