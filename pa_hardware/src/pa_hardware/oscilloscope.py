"""PicoScope 5000 series controller and mock."""
from __future__ import annotations
import ctypes
import numpy as np


# Voltage range index → (ps5000a enum value, full-scale volts)
_RANGES: dict[str, tuple[int, float]] = {
    "10 mV":  (1,  0.01),
    "20 mV":  (2,  0.02),
    "50 mV":  (3,  0.05),
    "100 mV": (4,  0.10),
    "200 mV": (5,  0.20),
    "500 mV": (6,  0.50),
    "1 V":    (7,  1.00),
    "2 V":    (8,  2.00),
    "5 V":    (9,  5.00),
    "10 V":   (10, 10.0),
    "20 V":   (11, 20.0),
}

_CHANNELS = {"A": 0, "B": 1, "C": 2, "D": 3}
_COUPLINGS = {"AC": 0, "DC": 1}

RANGE_LABELS = list(_RANGES.keys())
CHANNEL_LABELS = list(_CHANNELS.keys())
COUPLING_LABELS = list(_COUPLINGS.keys())


def _rate_to_timebase(rate_hz: float) -> int:
    """Convert desired sample rate to ps5000a timebase index (8-bit mode)."""
    if rate_hz >= 125e6:
        return 2
    if rate_hz >= 62.5e6:
        return 3
    # interval_s = (n - 2) / 62.5e6  →  n = ceil(62.5e6 / rate) + 2
    return int(62.5e6 / rate_hz) + 2


class OscilloscopeController:
    """PicoScope 5000a wrapper for triggered block capture.

    Requires the PicoScope 5000 Series PC Oscilloscope drivers to be
    installed on the system (available from picotech.com/downloads).
    """

    def __init__(self):
        self._handle = ctypes.c_int16(0)
        self._ps = None
        self._ok = None
        self._channel = "A"
        self._coupling = "DC"
        self._range = "500 mV"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        from picosdk.ps5000a import ps5000a as ps
        from picosdk.functions import assert_pico_ok

        self._ps = ps
        self._ok = assert_pico_ok
        # resolution = 1 → 8-bit (fastest, highest sample rate)
        self._ok(ps.ps5000aOpenUnit(ctypes.byref(self._handle), None, 1))
        self._apply_channel_config()

    def disconnect(self) -> None:
        if self._ps and self._handle.value:
            self._ps.ps5000aCloseUnit(self._handle)
            self._handle = ctypes.c_int16(0)

    @property
    def is_connected(self) -> bool:
        return self._handle.value != 0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure_channel(
        self,
        channel: str = "A",
        coupling: str = "DC",
        range_label: str = "500 mV",
        analog_offset: float = 0.0,
    ) -> None:
        self._channel = channel
        self._coupling = coupling
        self._range = range_label
        if self.is_connected:
            self._apply_channel_config(analog_offset)

    def _apply_channel_config(self, offset: float = 0.0) -> None:
        enum_val, _ = _RANGES[self._range]
        self._ok(
            self._ps.ps5000aSetChannel(
                self._handle,
                _CHANNELS[self._channel],
                1,  # enabled
                _COUPLINGS[self._coupling],
                enum_val,
                offset,
            )
        )

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
        n_samples = int(sample_rate_hz * duration_ms / 1e3)
        timebase = _rate_to_timebase(sample_rate_hz)
        _, range_v = _RANGES[self._range]

        # ADC count for trigger threshold (16-bit signed for ps5000a)
        adc_trigger = int(trigger_mv / 1e3 / range_v * 32767)
        adc_trigger = max(-32767, min(32767, adc_trigger))

        # Rising-edge trigger on active channel; auto-trigger after 1 s
        self._ok(
            self._ps.ps5000aSetSimpleTrigger(
                self._handle,
                1,  # enable
                _CHANNELS[self._channel],
                adc_trigger,
                2,     # rising edge
                0,     # pre-trigger samples
                1000,  # auto-trigger ms
            )
        )

        self._ok(
            self._ps.ps5000aRunBlock(
                self._handle, 0, n_samples, timebase, None, 0, None, None
            )
        )

        # Poll until ready
        ready = ctypes.c_int16(0)
        while not ready.value:
            self._ps.ps5000aIsReady(self._handle, ctypes.byref(ready))

        buf = (ctypes.c_int16 * n_samples)()
        self._ok(
            self._ps.ps5000aSetDataBuffer(
                self._handle,
                _CHANNELS[self._channel],
                ctypes.byref(buf),
                n_samples,
                0,
                0,
            )
        )

        n_got = ctypes.c_uint32(n_samples)
        overflow = ctypes.c_int16()
        self._ok(
            self._ps.ps5000aGetValues(
                self._handle, 0, ctypes.byref(n_got), 1, 0, 0,
                ctypes.byref(overflow)
            )
        )

        n = n_got.value
        voltage_mv = np.array(buf[:n], dtype=float) / 32767 * range_v * 1e3
        time_us = np.arange(n) / sample_rate_hz * 1e6
        return time_us, voltage_mv


class MockOscilloscopeController:
    """Simulated PicoScope — generates a realistic damped-sinusoid PA signal."""

    def __init__(self):
        self._channel = "A"
        self._range = "500 mV"
        self._rng = np.random.default_rng()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True

    def configure_channel(self, channel="A", coupling="DC",
                           range_label="500 mV", analog_offset=0.0) -> None:
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
