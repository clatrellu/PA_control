"""Cobolt laser controller (serial) and mock."""
from __future__ import annotations
import threading


class LaserController:
    """Cobolt laser over USB-serial (ASCII command protocol).

    Baud rate and exact power commands may differ between Cobolt models —
    verify against your model's user manual (08-01, 08-DPL, etc.).
    """

    BAUD = 115200
    TIMEOUT = 1.0  # seconds

    def __init__(self):
        self._serial = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, port: str) -> None:
        import serial
        self._serial = serial.Serial(
            port, baudrate=self.BAUD, timeout=self.TIMEOUT
        )
        self._serial.reset_input_buffer()

    def disconnect(self) -> None:
        if self._serial and self._serial.is_open:
            self.set_enabled(False)
            self._serial.close()
        self._serial = None

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._send("l1" if enabled else "l0")

    def get_enabled(self) -> bool:
        resp = self._query("l?")
        return resp.strip() == "1"

    def set_power(self, power_mw: float) -> None:
        # Cobolt 08-series: "@cobasrp <mW>" sets constant-power mode setpoint.
        # Some models use "p <mW>" instead — adjust if needed.
        self._send(f"@cobasrp {power_mw:.4f}")

    def get_power(self) -> float:
        """Return actual output power in mW."""
        resp = self._query("p?")
        return float(resp.strip())

    def set_modulation_mode(self, mode: str) -> None:
        """Set modulation mode: 'cw' (continuous wave) or 'external' (digital trigger).

        In 'external' mode the laser fires on the rising edge of the modulation
        input — wire the NI-DAQ counter output to that input.
        Command '@cobasd' is for Cobolt 08-series; verify against your model manual.
        """
        self._send("@cobasd 1" if mode == "external" else "@cobasd 0")

    def get_modulation_mode(self) -> str:
        resp = self._query("@cobasd?")
        return "external" if resp.strip() == "1" else "cw"

    def get_status(self) -> dict:
        return {
            "enabled": self.get_enabled(),
            "power_mw": self.get_power(),
            "modulation_mode": self.get_modulation_mode(),
        }

    # ------------------------------------------------------------------
    # Low-level serial
    # ------------------------------------------------------------------

    def _send(self, cmd: str) -> None:
        with self._lock:
            self._serial.write(f"{cmd}\r".encode())

    def _query(self, cmd: str) -> str:
        with self._lock:
            self._serial.write(f"{cmd}\r".encode())
            return self._serial.readline().decode(errors="replace")


class MockLaserController:
    """Simulated laser for UI development without hardware."""

    def __init__(self):
        self._enabled = False
        self._setpoint_mw = 0.0
        self._modulation_mode = "cw"

    def connect(self, port: str) -> None:
        pass

    def disconnect(self) -> None:
        self._enabled = False

    @property
    def is_connected(self) -> bool:
        return True

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def get_enabled(self) -> bool:
        return self._enabled

    def set_power(self, power_mw: float) -> None:
        self._setpoint_mw = float(power_mw)

    def get_power(self) -> float:
        return self._setpoint_mw if self._enabled else 0.0

    def set_modulation_mode(self, mode: str) -> None:
        self._modulation_mode = mode

    def get_modulation_mode(self) -> str:
        return self._modulation_mode

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "power_mw": self.get_power(),
            "modulation_mode": self._modulation_mode,
        }
