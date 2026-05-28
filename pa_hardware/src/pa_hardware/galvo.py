"""Thorlabs galvanometric mirror controller via NI-DAQ analog outputs."""
from __future__ import annotations
import threading
import numpy as np


class GalvoController:
    """Controls X/Y galvo mirrors through two NI-DAQ analog output channels.

    Typical channel names: 'Dev1/ao0' (X) and 'Dev1/ao1' (Y).
    Voltage range ±10 V maps to the mirror's full angular range.
    """

    V_MIN = -10.0
    V_MAX = 10.0

    def __init__(self):
        self._task_x = None
        self._task_y = None
        self._lock = threading.Lock()
        self._x = 0.0
        self._y = 0.0

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, x_channel: str, y_channel: str) -> None:
        import nidaqmx

        self._task_x = nidaqmx.Task()
        self._task_x.ao_channels.add_ao_voltage_chan(
            x_channel, min_val=self.V_MIN, max_val=self.V_MAX
        )
        self._task_y = nidaqmx.Task()
        self._task_y.ao_channels.add_ao_voltage_chan(
            y_channel, min_val=self.V_MIN, max_val=self.V_MAX
        )
        self.move_to(0.0, 0.0)

    def disconnect(self) -> None:
        self.move_to(0.0, 0.0)
        for task in (self._task_x, self._task_y):
            if task is not None:
                task.close()
        self._task_x = None
        self._task_y = None

    @property
    def is_connected(self) -> bool:
        return self._task_x is not None

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def move_to(self, x_v: float, y_v: float) -> None:
        x_v = float(np.clip(x_v, self.V_MIN, self.V_MAX))
        y_v = float(np.clip(y_v, self.V_MIN, self.V_MAX))
        with self._lock:
            self._task_x.write(x_v)
            self._task_y.write(y_v)
            self._x, self._y = x_v, y_v

    def get_position(self) -> tuple[float, float]:
        return self._x, self._y

    def center(self) -> None:
        self.move_to(0.0, 0.0)


class MockGalvoController:
    """Simulated galvo for UI development without hardware."""

    V_MIN = -10.0
    V_MAX = 10.0

    def __init__(self):
        self._x = 0.0
        self._y = 0.0

    def connect(self, x_channel: str, y_channel: str) -> None:
        pass

    def disconnect(self) -> None:
        self._x = 0.0
        self._y = 0.0

    @property
    def is_connected(self) -> bool:
        return True

    def move_to(self, x_v: float, y_v: float) -> None:
        self._x = float(np.clip(x_v, self.V_MIN, self.V_MAX))
        self._y = float(np.clip(y_v, self.V_MIN, self.V_MAX))

    def get_position(self) -> tuple[float, float]:
        return self._x, self._y

    def center(self) -> None:
        self.move_to(0.0, 0.0)
