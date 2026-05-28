"""NI-DAQ counter-output trigger for laser repetition-rate control."""
from __future__ import annotations


class TriggerController:
    """Generates a periodic TTL output via NI-DAQ counter output channel.

    Wire the counter output terminal to the Cobolt laser's external
    modulation/trigger input. Default output terminal for ctr0 varies by
    device (e.g. PFI12 on USB-6211, PFI4 on USB-6361) — check NI-MAX.

    The laser must also be placed in digital-modulation mode; use
    LaserController.set_modulation_mode('external') before calling start().
    """

    def __init__(self):
        self._channel: str | None = None
        self._task = None
        self._freq_hz = 1000.0
        self._duty = 0.05

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, channel: str) -> None:
        """Register the counter channel (e.g. 'Dev1/ctr0'). No task opened yet."""
        self._channel = channel

    def disconnect(self) -> None:
        self.stop()
        self._channel = None

    @property
    def is_connected(self) -> bool:
        return self._channel is not None

    @property
    def is_running(self) -> bool:
        return self._task is not None

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self, freq_hz: float, duty_cycle: float = 0.05) -> None:
        """Start generating trigger pulses at freq_hz with given duty cycle."""
        import nidaqmx
        from nidaqmx.constants import AcquisitionType

        self.stop()
        self._freq_hz = freq_hz
        self._duty = duty_cycle

        self._task = nidaqmx.Task()
        self._task.co_channels.add_co_pulse_chan_freq(
            self._channel,
            freq=freq_hz,
            duty_cycle=duty_cycle,
        )
        self._task.timing.cfg_implicit_timing(
            sample_mode=AcquisitionType.CONTINUOUS
        )
        self._task.start()

    def stop(self) -> None:
        if self._task is not None:
            try:
                self._task.stop()
                self._task.close()
            except Exception:
                pass
            self._task = None

    def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "freq_hz": self._freq_hz,
            "duty_cycle": self._duty,
        }


class MockTriggerController:
    """Simulated trigger for development without hardware."""

    def __init__(self):
        self._channel: str | None = None
        self._running = False
        self._freq_hz = 1000.0
        self._duty = 0.05

    def connect(self, channel: str) -> None:
        self._channel = channel

    def disconnect(self) -> None:
        self._running = False
        self._channel = None

    @property
    def is_connected(self) -> bool:
        return self._channel is not None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, freq_hz: float, duty_cycle: float = 0.05) -> None:
        self._freq_hz = freq_hz
        self._duty = duty_cycle
        self._running = True

    def stop(self) -> None:
        self._running = False

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "freq_hz": self._freq_hz,
            "duty_cycle": self._duty,
        }
