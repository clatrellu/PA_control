"""Zaber T-LLS105 linear translation stage via Binary Protocol over USB serial.

Talks directly to the device's Binary Protocol (6-byte frames: address,
command, 4-byte little-endian signed data) rather than the zaber_motion
library — ported from /home/xray/zaber-gui/zaber_device.py, where
zaber_motion's bundled connection layer was found to time out against this
particular USB-serial adapter even though the device answers raw frames
fine at 9600 baud.

Command codes are taken verbatim from zaber_motion.binary.CommandCode.

Position is exposed in millimetres; steps are the device's native unit.
The mm<->step conversion below is the T-LLS105's default microstep size
(0.15625 um/microstep, i.e. 1/64 step resolution, per Zaber's published
spec) and is only correct if that device setting hasn't been changed.
"""
from __future__ import annotations
import struct
import threading
import time

import serial

_CMD_HOME = 1
_CMD_MOVE_ABSOLUTE = 20
_CMD_MOVE_RELATIVE = 21
_CMD_STOP = 23
_CMD_SET_TARGET_SPEED = 42
_CMD_RETURN_FIRMWARE_VERSION = 51
_CMD_RETURN_SETTING = 53
_CMD_RETURN_STATUS = 54
_CMD_RETURN_CURRENT_POSITION = 60

_SETTING_TARGET_SPEED = 42  # setting number for RETURN_SETTING, not a command

# A handful of RETURN_STATUS codes worth naming for the UI; anything else is
# still a legitimate busy/mode code, just shown as a number.
STATUS_NAMES = {
    0: "idle",
    1: "homing",
    10: "moving (constant speed)",
    20: "moving (absolute)",
    21: "moving (relative)",
    65: "manual move (knob)",
}

MM_PER_STEP = 0.00015625  # 0.15625 um/microstep at default 1/64 resolution
# Target Speed data unit, from Zaber's T-LLS105 spec (speed resolution;
# max 6 mm/s ~= 4096 units). Same default-resolution caveat as MM_PER_STEP.
MM_S_PER_SPEED_UNIT = 0.001465
MAX_SPEED_MM_S = 6.0
TRAVEL_MM = 105.0
DEFAULT_ADDRESS = 2
DEFAULT_BAUDRATE = 9600


class ZaberBinaryError(Exception):
    pass


class StageController:
    """Zaber T-LLS105 linear stage over USB serial (Binary Protocol)."""

    TRAVEL_MM = TRAVEL_MM
    MM_PER_STEP = MM_PER_STEP

    def __init__(self):
        self._serial: serial.Serial | None = None
        self._address = DEFAULT_ADDRESS
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(
        self,
        port: str,
        address: int = DEFAULT_ADDRESS,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 1.0,
    ) -> None:
        self._address = address
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = serial.Serial(
                port, baudrate, timeout=timeout,
                rtscts=False, dsrdtr=False, xonxoff=False,
            )
            time.sleep(0.2)
            self._serial.reset_input_buffer()

    def disconnect(self) -> None:
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _send(self, command: int, data: int = 0) -> tuple[int, int, int]:
        if not self.is_connected:
            raise ZaberBinaryError("Not connected to stage")
        with self._lock:
            frame = struct.pack("<BBl", self._address, command, int(data))
            self._serial.reset_input_buffer()
            self._serial.write(frame)
            resp = self._serial.read(6)
            if len(resp) < 6:
                raise ZaberBinaryError(
                    f"No response from stage at address {self._address} "
                    f"(command {command})"
                )
            dev, rcmd, rdata = struct.unpack("<BBl", resp)
            if rcmd == 255:
                raise ZaberBinaryError(f"Stage returned error code {rdata}")
            return dev, rcmd, rdata

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def home(self) -> None:
        self._send(_CMD_HOME, 0)

    def stop(self) -> None:
        self._send(_CMD_STOP, 0)

    def move_absolute_mm(self, position_mm: float) -> None:
        self._send(_CMD_MOVE_ABSOLUTE, round(position_mm / MM_PER_STEP))

    def move_relative_mm(self, delta_mm: float) -> None:
        self._send(_CMD_MOVE_RELATIVE, round(delta_mm / MM_PER_STEP))

    def get_position_mm(self) -> float:
        _, _, steps = self._send(_CMD_RETURN_CURRENT_POSITION, 0)
        return steps * MM_PER_STEP

    def get_status(self) -> int:
        _, _, status = self._send(_CMD_RETURN_STATUS, 0)
        return status

    def is_busy(self) -> bool:
        return self.get_status() != 0

    def get_firmware_version(self) -> float:
        _, _, v = self._send(_CMD_RETURN_FIRMWARE_VERSION, 0)
        return v / 100.0

    def get_target_speed_raw(self) -> int:
        """Current Target Speed setting, in the device's own native units —
        no absolute mm/s conversion is exposed. Callers that just need to
        temporarily slow a move down should scale this raw value rather
        than compute an absolute speed (see set_target_speed_raw)."""
        _, _, v = self._send(_CMD_RETURN_SETTING, _SETTING_TARGET_SPEED)
        return v

    def set_target_speed_raw(self, value: int) -> None:
        self._send(_CMD_SET_TARGET_SPEED, int(value))

    def get_target_speed_mm_s(self) -> float:
        return self.get_target_speed_raw() * MM_S_PER_SPEED_UNIT

    def set_target_speed_mm_s(self, speed_mm_s: float) -> None:
        self.set_target_speed_raw(max(1, round(speed_mm_s / MM_S_PER_SPEED_UNIT)))


class MockStageController:
    """Simulated Zaber stage for UI development without hardware."""

    TRAVEL_MM = TRAVEL_MM
    MM_PER_STEP = MM_PER_STEP

    def __init__(self):
        self._connected = False
        self._position_mm = 0.0
        self._target_speed_raw = 1000

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(
        self,
        port: str,
        address: int = DEFAULT_ADDRESS,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 1.0,
    ) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def home(self) -> None:
        self._position_mm = 0.0

    def stop(self) -> None:
        pass

    def move_absolute_mm(self, position_mm: float) -> None:
        time.sleep(0.05)
        self._position_mm = position_mm

    def move_relative_mm(self, delta_mm: float) -> None:
        time.sleep(0.05)
        self._position_mm += delta_mm

    def get_position_mm(self) -> float:
        return self._position_mm

    def get_status(self) -> int:
        return 0

    def is_busy(self) -> bool:
        return False

    def get_firmware_version(self) -> float:
        return 7.34

    def get_target_speed_raw(self) -> int:
        return self._target_speed_raw

    def set_target_speed_raw(self, value: int) -> None:
        self._target_speed_raw = int(value)

    def get_target_speed_mm_s(self) -> float:
        return self._target_speed_raw * MM_S_PER_SPEED_UNIT

    def set_target_speed_mm_s(self, speed_mm_s: float) -> None:
        self._target_speed_raw = max(1, round(speed_mm_s / MM_S_PER_SPEED_UNIT))
