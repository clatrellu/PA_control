"""Galvo mirror control panel widget."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDoubleSpinBox, QSlider, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal


class GalvoWidget(QGroupBox):
    """X/Y galvo position control (voltage, ±10 V)."""

    position_changed = pyqtSignal(float, float)   # x_v, y_v
    center_requested = pyqtSignal()

    V_MIN = -10.0
    V_MAX = 10.0
    SLIDER_SCALE = 100  # slider integer units per volt

    def __init__(self, parent=None):
        super().__init__("Galvo Mirrors", parent)
        self._building = True
        self._setup_ui()
        self._building = False
        self.set_connected(False)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        self._x_slider, self._x_spin = self._make_axis_row(layout, "X")
        self._y_slider, self._y_spin = self._make_axis_row(layout, "Y")

        btn_center = QPushButton("Center (0 V, 0 V)")
        btn_center.setFixedHeight(30)
        btn_center.clicked.connect(self._on_center)
        layout.addWidget(btn_center)

    def _make_axis_row(
        self, parent_layout: QVBoxLayout, label: str
    ) -> tuple[QSlider, QDoubleSpinBox]:
        parent_layout.addWidget(QLabel(f"{label} position:"))
        row = QHBoxLayout()

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(
            int(self.V_MIN * self.SLIDER_SCALE),
            int(self.V_MAX * self.SLIDER_SCALE),
        )
        slider.setValue(0)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        spin = QDoubleSpinBox()
        spin.setRange(self.V_MIN, self.V_MAX)
        spin.setSingleStep(0.01)
        spin.setDecimals(2)
        spin.setSuffix(" V")
        spin.setFixedWidth(85)

        # Cross-link slider ↔ spinbox
        slider.valueChanged.connect(
            lambda v, s=spin: self._slider_to_spin(v, s)
        )
        spin.valueChanged.connect(
            lambda v, s=slider: self._spin_to_slider(v, s)
        )

        row.addWidget(slider)
        row.addWidget(spin)
        parent_layout.addLayout(row)
        return slider, spin

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        for w in (self._x_slider, self._x_spin, self._y_slider, self._y_spin):
            w.setEnabled(connected)
        self.findChild(QPushButton).setEnabled(connected)

    def update_position(self, x_v: float, y_v: float) -> None:
        for spin, val in ((self._x_spin, x_v), (self._y_spin, y_v)):
            spin.blockSignals(True)
            spin.setValue(val)
            spin.blockSignals(False)
        for slider, val in (
            (self._x_slider, x_v), (self._y_slider, y_v)
        ):
            slider.blockSignals(True)
            slider.setValue(int(val * self.SLIDER_SCALE))
            slider.blockSignals(False)

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _slider_to_spin(self, value: int, spin: QDoubleSpinBox) -> None:
        v = value / self.SLIDER_SCALE
        spin.blockSignals(True)
        spin.setValue(v)
        spin.blockSignals(False)
        if not self._building:
            self._emit_position()

    def _spin_to_slider(self, value: float, slider: QSlider) -> None:
        slider.blockSignals(True)
        slider.setValue(int(value * self.SLIDER_SCALE))
        slider.blockSignals(False)
        if not self._building:
            self._emit_position()

    def _emit_position(self) -> None:
        self.position_changed.emit(self._x_spin.value(), self._y_spin.value())

    def _on_center(self) -> None:
        self.update_position(0.0, 0.0)
        self.center_requested.emit()
