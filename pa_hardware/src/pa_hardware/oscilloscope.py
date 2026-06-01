"""Oscilloscope controllers: Red Pitaya STEM 125-10 and PicoScope 5444D MSO."""
from __future__ import annotations
import socket
import time
from typing import Any
import numpy as np

# ---------------------------------------------------------------------------
# Red Pitaya STEM 125-10
# ---------------------------------------------------------------------------

_RP_CLOCK_HZ: float = 125e6  # 125 MHz ADC clock
_BUFFER_SIZE: int = 16384    # hardware buffer depth (samples)

# Input gain settings → (SCPI gain string, full-scale volts peak)
_RANGES: dict[str, tuple[str, float]] = {
    "1 V":  ("LV", 1.0),
    "20 V": ("HV", 20.0),
}

_CHANNELS = {"CH1": 1, "CH2": 2}
_COUPLINGS = {"DC": "DC", "AC": "AC"}

# Module-level constants kept for backward compatibility
RANGE_LABELS = list(_RANGES.keys())
CHANNEL_LABELS = list(_CHANNELS.keys())
COUPLING_LABELS = list(_COUPLINGS.keys())

RP_SAMPLE_RATES: dict[str, float] = {
    "125 MS/s":   125e6,
    "62.5 MS/s":  62.5e6,
    "31.25 MS/s": 31.25e6,
    "10 MS/s":    10e6,
    "1 MS/s":     1e6,
}

RP_DEFAULT_IP ="192.168.1.100"
 #"169.254.127.245"


def _rate_to_decimation(rate_hz: float) -> int:
    """Return the smallest integer decimation that achieves >= rate_hz (clamped 1–65536)."""
    return max(1, min(65536, round(_RP_CLOCK_HZ / rate_hz)))


class OscilloscopeController:
    """Red Pitaya STEM 125-10 wrapper for triggered block capture via SCPI over TCP.

    Requires the SCPI server to be running on the Red Pitaya (enabled by
    default in the latest Red Pitaya OS; accessible on port 5000).
    """

    CHANNEL_LABELS = CHANNEL_LABELS
    COUPLING_LABELS = COUPLING_LABELS
    RANGE_LABELS = RANGE_LABELS
    SAMPLE_RATES = RP_SAMPLE_RATES

    def __init__(self, ip: str = RP_DEFAULT_IP, port: int = 5000, timeout: float = 10.0):
        self._ip = ip
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._channel = "CH1"
        self._coupling = "DC"
        self._range = "1 V"

    # ------------------------------------------------------------------
    # SCPI transport
    # ------------------------------------------------------------------

    def _send(self, cmd: str) -> None:
        assert self._sock is not None, "Not connected"
        self._sock.sendall((cmd + "\r\n").encode())

    def _recv(self) -> str:
        assert self._sock is not None, "Not connected"
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = self._sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode().strip()

    def _ask(self, cmd: str) -> str:
        self._send(cmd)
        return self._recv()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        self._sock.connect((self._ip, self._port))
        self._send("ACQ:RST")
        self._apply_channel_config()

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure_channel(
        self,
        channel: str = "CH1",
        coupling: str = "DC",
        range_label: str = "1 V",
        analog_offset: float = 0.0,
    ) -> None:
        self._channel = channel
        self._coupling = coupling
        self._range = range_label
        if self.is_connected:
            self._apply_channel_config()

    def _apply_channel_config(self) -> None:
        ch = _CHANNELS[self._channel]
        gain, _ = _RANGES[self._range]
        self._send(f"ACQ:SOUR{ch}:GAIN {gain}")

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Triggered single-block capture.

        Returns (time_us, voltage_mv) as NumPy arrays.
        """
        decimation = _rate_to_decimation(sample_rate_hz)
        actual_rate = _RP_CLOCK_HZ / decimation
        n_samples = min(int(actual_rate * duration_ms / 1e3), _BUFFER_SIZE)
        ch = _CHANNELS[self._channel]
        trig_v = trigger_mv / 1e3

        self._send("ACQ:RST")
        self._apply_channel_config()
        self._send(f"ACQ:DEC {decimation}")
        self._send(f"ACQ:TRIG:LEV {trig_v:.6f}")
        # DLY = n_samples: capture exactly n_samples post-trigger before stopping
        self._send(f"ACQ:TRIG:DLY {n_samples}")
        self._send("ACQ:START")

        if trigger_mv == 0.0:
            # Immediate capture — no edge detection, always returns data
            self._send("ACQ:TRIG NOW")
        else:
            # Wait for rising edge at the requested threshold (max 5 s)
            self._send(f"ACQ:TRIG CH{ch}_PE")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self._ask("ACQ:TRIG:STAT?") == "TD":
                    break
                time.sleep(0.005)
            else:
                raise TimeoutError("Red Pitaya: trigger not detected within 5 s")

        # Wait for post-trigger samples to fill at actual_rate
        time.sleep(n_samples / actual_rate + 0.005)

        # Read data from trigger position, handling circular buffer wrap-around
        trig_pos = int(self._ask("ACQ:TRIG:POS?"))
        if trig_pos + n_samples <= _BUFFER_SIZE:
            raw = self._ask(f"ACQ:SOUR{ch}:DATA:STA:N? {trig_pos},{n_samples}")
            voltage_v = np.fromstring(raw.strip("{}"), sep=",", dtype=float)
        else:
            n1 = _BUFFER_SIZE - trig_pos
            n2 = n_samples - n1
            r1 = self._ask(f"ACQ:SOUR{ch}:DATA:STA:N? {trig_pos},{n1}")
            r2 = self._ask(f"ACQ:SOUR{ch}:DATA:STA:N? 0,{n2}")
            v1 = np.fromstring(r1.strip("{}"), sep=",", dtype=float)
            v2 = np.fromstring(r2.strip("{}"), sep=",", dtype=float)
            voltage_v = np.concatenate([v1, v2])

        n = len(voltage_v)
        time_us = np.arange(n) / actual_rate * 1e6
        return time_us, voltage_v * 1e3


class MockOscilloscopeController:
    """Simulated Red Pitaya — generates a realistic damped-sinusoid PA signal."""

    CHANNEL_LABELS = CHANNEL_LABELS
    COUPLING_LABELS = COUPLING_LABELS
    RANGE_LABELS = RANGE_LABELS
    SAMPLE_RATES = RP_SAMPLE_RATES

    def __init__(self):
        self._channel = "CH1"
        self._range = "1 V"
        self._rng = np.random.default_rng()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True

    def configure_channel(self, channel="CH1", coupling="DC",
                           range_label="1 V", analog_offset=0.0) -> None:
        self._channel = channel
        self._range = range_label

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        n = int(sample_rate_hz * duration_ms / 1e3)
        t_us = np.linspace(0, duration_ms * 1e3, n)

        # ~10 MHz damped sinusoid (photoacoustic-like) + Gaussian noise
        freq_hz = 10e6
        decay_us = duration_ms * 100
        signal = (
            120.0
            * np.exp(-t_us / decay_us)
            * np.sin(2 * np.pi * freq_hz * t_us / 1e6)
        )
        noise = self._rng.normal(0, 8, n)
        return t_us, signal + noise


# ---------------------------------------------------------------------------
# PicoScope 5444D MSO  (ps5000a driver, USB)
# ---------------------------------------------------------------------------

# Channel indices (PS5000A_CHANNEL enum)
_PICO_CHANNELS: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3}

# Coupling indices (PS5000A_COUPLING enum): AC=0, DC=1
_PICO_COUPLINGS: dict[str, int] = {"AC": 0, "DC": 1}

# Range index → (PS5000A_RANGE enum value, full-scale volts)
_PICO_RANGES: dict[str, tuple[int, float]] = {
    "10 mV":  (0,  0.010),
    "20 mV":  (1,  0.020),
    "50 mV":  (2,  0.050),
    "100 mV": (3,  0.100),
    "200 mV": (4,  0.200),
    "500 mV": (5,  0.500),
    "1 V":    (6,  1.0),
    "2 V":    (7,  2.0),
    "5 V":    (8,  5.0),
    "10 V":   (9,  10.0),
    "20 V":   (10, 20.0),
}

# 12-bit resolution (PS5000A_DR_12BIT = 1); max ADC value for ps5000a
_PICO_RESOLUTION: int = 1
_PICO_MAX_ADC: int = 32512

PICO_CHANNEL_LABELS = list(_PICO_CHANNELS.keys())
PICO_COUPLING_LABELS = list(_PICO_COUPLINGS.keys())
PICO_RANGE_LABELS = list(_PICO_RANGES.keys())

PICO_SAMPLE_RATES: dict[str, float] = {
    "500 MS/s":   500e6,
    "250 MS/s":   250e6,
    "125 MS/s":   125e6,
    "62.5 MS/s":  62.5e6,
    "31.25 MS/s": 31.25e6,
    "10 MS/s":    10e6,
    "1 MS/s":     1e6,
}


def _rate_to_pico_timebase(rate_hz: float) -> int:
    """Map a desired sample rate to the nearest ps5000a timebase at 12-bit resolution.

    At 12-bit: timebase 1 → 500 MS/s, timebase 2 → 250 MS/s,
    timebase n ≥ 3 → 125e6/(n-2) S/s.
    """
    if rate_hz >= 500e6:
        return 1
    if rate_hz >= 250e6:
        return 2
    return max(3, round(125e6 / rate_hz + 2))


class PicoScope5444DController:
    """PicoScope 5444D MSO controller via the picosdk ps5000a driver (USB).

    Requires the PicoScope SDK and ``picosdk-python-wrappers`` to be installed.
    Connects to the first available device on USB automatically.
    """

    CHANNEL_LABELS = PICO_CHANNEL_LABELS
    COUPLING_LABELS = PICO_COUPLING_LABELS
    RANGE_LABELS = PICO_RANGE_LABELS
    SAMPLE_RATES = PICO_SAMPLE_RATES

    def __init__(self) -> None:
        import ctypes
        self._handle = ctypes.c_int16(0)
        self._channel = "A"
        self._coupling = "DC"
        self._range = "1 V"
        self._connected = False
        self._ps: Any = None  # ps5000a module, assigned on connect

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        import ctypes
        from ctypes.util import find_library
        # Pre-load libpicoipp.so so it is already in the linker cache when
        # libps5000a.so tries to dlopen it internally at first use.
        _picoipp = find_library("picoipp")
        if _picoipp:
            ctypes.cdll.LoadLibrary(_picoipp)
        from picosdk.ps5000a import ps5000a as ps
        from picosdk.functions import assert_pico_ok
        from picosdk.constants import PICO_STATUS

        status = ps.ps5000aOpenUnit(  # type: ignore[attr-defined]
            ctypes.byref(self._handle), None, _PICO_RESOLUTION
        )
        # 5444D can run on USB power alone; handle the power-supply-not-connected case
        if status == PICO_STATUS["PICO_POWER_SUPPLY_NOT_CONNECTED"]:
            status = ps.ps5000aChangePowerSource(  # type: ignore[attr-defined]
                self._handle, PICO_STATUS["PICO_POWER_SUPPLY_NOT_CONNECTED"]
            )
        assert_pico_ok(status)
        self._ps = ps
        self._connected = True
        self._apply_channel_config()

    def disconnect(self) -> None:
        if self._connected and self._ps is not None:
            try:
                self._ps.ps5000aStop(self._handle)
                self._ps.ps5000aCloseUnit(self._handle)
            finally:
                self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure_channel(
        self,
        channel: str = "A",
        coupling: str = "DC",
        range_label: str = "1 V",
        analog_offset: float = 0.0,
    ) -> None:
        self._channel = channel
        self._coupling = coupling
        self._range = range_label
        if self._connected:
            self._apply_channel_config()

    def _apply_channel_config(self) -> None:
        from picosdk.functions import assert_pico_ok

        coup_idx = _PICO_COUPLINGS[self._coupling]
        range_idx, _ = _PICO_RANGES[self._range]
        for ch_label, ch_idx in _PICO_CHANNELS.items():
            enabled = 1 if ch_label == self._channel else 0
            status = self._ps.ps5000aSetChannel(
                self._handle, ch_idx, enabled, coup_idx, range_idx, 0.0
            )
            assert_pico_ok(status)

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Triggered single-block capture.

        Returns (time_us, voltage_mv) as NumPy arrays.
        """
        import ctypes
        from picosdk.functions import adc2mV, assert_pico_ok, mV2adc

        timebase = ctypes.c_uint32(_rate_to_pico_timebase(sample_rate_hz))

        # Resolve actual timing and buffer capacity
        time_interval_ns = ctypes.c_float()
        max_samples = ctypes.c_int32()
        status = self._ps.ps5000aGetTimebase2(
            self._handle, timebase, 0,
            ctypes.byref(time_interval_ns), ctypes.byref(max_samples), 0,
        )
        assert_pico_ok(status)

        actual_rate_hz = 1e9 / time_interval_ns.value
        n_samples = min(
            int(actual_rate_hz * duration_ms / 1e3),
            max_samples.value,
            10_000_000,  # 10 M sample safety cap
        )

        ch_idx = _PICO_CHANNELS[self._channel]
        range_idx, _ = _PICO_RANGES[self._range]
        maxADC = ctypes.c_int16(_PICO_MAX_ADC)

        # Configure trigger (rising edge, auto-trigger after 1 s)
        threshold_adc = mV2adc(trigger_mv, range_idx, maxADC)
        status = self._ps.ps5000aSetSimpleTrigger(
            self._handle,
            1,               # enable
            ch_idx,          # source
            threshold_adc,   # threshold in ADC counts
            2,               # direction: PS5000A_RISING
            0,               # delay
            1000,            # autoTrigger_ms
        )
        assert_pico_ok(status)

        # Arm and wait for block capture
        time_indisposed = ctypes.c_int32()
        status = self._ps.ps5000aRunBlock(
            self._handle, 0, n_samples, timebase,
            ctypes.byref(time_indisposed), 0, None, None,
        )
        assert_pico_ok(status)

        ready = ctypes.c_int16(0)
        deadline = time.monotonic() + 5.0
        while ready.value == 0:
            self._ps.ps5000aIsReady(self._handle, ctypes.byref(ready))
            if time.monotonic() > deadline:
                raise TimeoutError("PicoScope 5444D: trigger not detected within 5 s")
            time.sleep(0.005)

        # Read back samples
        buf = (ctypes.c_int16 * n_samples)()
        status = self._ps.ps5000aSetDataBuffer(
            self._handle, ch_idx, ctypes.byref(buf), n_samples, 0, 0
        )
        assert_pico_ok(status)

        c_n_samples = ctypes.c_uint32(n_samples)
        overflow = ctypes.c_int16()
        status = self._ps.ps5000aGetValues(
            self._handle, 0, ctypes.byref(c_n_samples),
            1, 0, 0, ctypes.byref(overflow),
        )
        assert_pico_ok(status)

        n = c_n_samples.value
        voltage_mv = np.array(adc2mV(buf, range_idx, maxADC)[:n], dtype=float)
        time_us = np.arange(n) / actual_rate_hz * 1e6
        return time_us, voltage_mv


class MockPicoScope5444DController:
    """Simulated PicoScope 5444D — generates a realistic damped-sinusoid PA signal."""

    CHANNEL_LABELS = PICO_CHANNEL_LABELS
    COUPLING_LABELS = PICO_COUPLING_LABELS
    RANGE_LABELS = PICO_RANGE_LABELS
    SAMPLE_RATES = PICO_SAMPLE_RATES

    def __init__(self) -> None:
        self._channel = "A"
        self._range = "1 V"
        self._rng = np.random.default_rng()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True

    def configure_channel(
        self,
        channel: str = "A",
        coupling: str = "DC",
        range_label: str = "1 V",
        analog_offset: float = 0.0,
    ) -> None:
        self._channel = channel
        self._range = range_label

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        n = int(sample_rate_hz * duration_ms / 1e3)
        t_us = np.linspace(0, duration_ms * 1e3, n)

        freq_hz = 10e6
        decay_us = duration_ms * 100
        signal = (
            120.0
            * np.exp(-t_us / decay_us)
            * np.sin(2 * np.pi * freq_hz * t_us / 1e6)
        )
        noise = self._rng.normal(0, 8, n)
        return t_us, signal + noise
