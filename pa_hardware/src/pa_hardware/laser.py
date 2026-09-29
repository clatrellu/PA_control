"""Cobolt Tor XE 532 nm laser controller (via pycobolt, USB) and mock.

The Tor XE is a passively Q-switched, actively triggered nanosecond-pulse
laser: pulse energy (250 uJ at 532 nm) and pulse duration are fixed by the
cavity, and there is no CW mode or power setpoint. The only controls are
on/off, trigger source (internal / external / gated), and the internal
trigger's repetition rate — see the Cobolt Tor XE manual (D0444-F),
Sections 7.1 and 8.3.

pycobolt (https://github.com/cobolt-lasers/pycobolt) has no dedicated Tor XE
subclass — it only auto-classifies CW diode models (Cobolt06/06MLD/06DPL), so
it stays on the generic CoboltLaser base class here. Q-switched-specific
commands (trigger source, internal rep rate) are sent via `send_cmd` using
the command set from the manual's Section 8.3; on/off, interlock, and fault
handling use pycobolt's built-in methods, which match that same command set.

Connect using the laser head's USB port (mini-B) — this enumerates as a
virtual COM port (e.g. "COM5" on Windows, "/dev/ttyACM0" on Linux) at the
same 115200 baud pycobolt uses by default.
"""
from __future__ import annotations
import threading
from contextlib import contextmanager

_TRIGGER_SOURCES = ("internal", "external", "gated")

# `gom?` is confirmed NOT in the Tor XE manual's command table (D0444-F
# Section 8.3, page 36) — it's an undocumented/internal query, so this
# mapping is inference, not documented fact. The manual's Cobolt Monitor
# screens (pages 11 & 14) do document the real autostart sequence by name,
# though, which the raw codes below are presumably numbering:
#   Off -> Waiting for Temp -> Waiting for Key -> Warming up -> Completed
#   (Fault and Aborted are also possible, off that main path)
# pycobolt's Cobolt06DPL.get_state() numbering (a different, CW model) was
# tried here first and shown wrong for two of its three testable entries
# against a real Tor XE unit — that borrowed table was discarded. A previous
# entry here, "7" = "Modulation", was also wrong — "Modulation" isn't a state
# in the real sequence at all; "7" is now confirmed (see below) to be "Off",
# observed at connect before any key action, matching the sequence's first
# stage. Only codes actually observed and confirmed on this hardware get a
# name; anything else displays as "unknown (raw N)" rather than a guessed
# label. Informational only — never used to gate `is_ready()` or any other
# control-flow decision.
_OPERATING_STATES = {
    "1": "Waiting for Temp",  # confirmed: observed after a key cycle, before progressing on its own to "Waiting for key"
    "2": "Waiting for key",   # confirmed: shown while the physical key is off, and reached again after "Waiting for Temp" clears
    "5": "Completed",         # confirmed: shown while armed/actively emitting
    "7": "Off",               # confirmed: shown at connect, before any key action — matches the real sequence's first stage
}

# Real fault codes from the manual's Communication Commands table (D0444-F,
# Section 8.3, page 36) — `f?` returns one of these (or "no fault").
_FAULT_CODES = {
    "3002": "pulse repetition rate out of range",
    "5001": "interlock fault",
}


def _decode_fault(raw: str) -> str:
    """Expand a raw f? response into a human-readable fault message."""
    code = raw.strip()
    if code.lower() == "no fault":
        return code
    if code in _FAULT_CODES:
        return f"{code} ({_FAULT_CODES[code]})"
    if code.startswith("700"):
        # "700X" is documented as "temperature fault" without listing every X
        return f"{code} (temperature fault)"
    return f"{code} (unrecognized fault code)"


class LaserController:
    """Cobolt Tor XE 532 nm over USB (virtual COM port, ASCII command protocol)."""

    def __init__(self):
        self._laser = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, port: str) -> None:
        import pycobolt
        self._laser = pycobolt.CoboltLaser(port=port)

    @contextmanager
    def _exchange(self):
        """Acquire the lock and flush stale bytes before a serial exchange.

        pycobolt's send_cmd() reads with a 1 s timeout; if the laser is slow
        to answer (it can be, mid warm-up/state-transition) the real reply
        arrives after that timeout and sits unread in the OS receive buffer.
        The *next* command's readline() then returns those stale bytes
        instead of its own fresh response — silently answering the wrong
        query. This showed up as get_repetition_rate() returning
        unrelated-looking values, and as get_enabled() reporting OFF while
        the laser was actually firing. Flushing right before every exchange
        guarantees each read is for the command just sent.
        """
        with self._lock:
            if self._laser is not None and self._laser.address is not None:
                self._laser.address.reset_input_buffer()
            yield

    def disconnect(self) -> None:
        if self._laser is not None:
            self.set_enabled(False)
            self._laser.disconnect()
        self._laser = None

    @property
    def is_connected(self) -> bool:
        return self._laser is not None and self._laser.is_connected()

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        with self._exchange():
            if enabled:
                self._laser.turn_on()
            else:
                self._laser.turn_off()

    def get_enabled(self) -> bool:
        with self._exchange():
            return self._laser.is_on()

    def set_trigger_source(self, source: str) -> None:
        """Set trigger source to 'internal', 'external', or 'gated'.

        Use 'external' when the laser is fired by TriggerController's
        NI-DAQ counter output — the laser emits one optical pulse per
        rising edge on its "Trig In" input.
        """
        if source not in _TRIGGER_SOURCES:
            raise ValueError(f"invalid trigger source: {source!r}")
        with self._exchange():
            self._laser.send_cmd(f"slt {source}")

    def get_trigger_source(self) -> str:
        with self._exchange():
            return self._laser.send_cmd("glt?").strip().lower()

    def set_internal_rate(self, rate_hz: float) -> None:
        """Set the repetition rate (Hz) used by the internal/gated trigger source."""
        with self._exchange():
            self._laser.send_cmd(f"sif {round(rate_hz)}")

    def get_internal_rate_setpoint(self) -> float:
        with self._exchange():
            return float(self._laser.send_cmd("gif?"))

    def get_repetition_rate(self) -> float:
        """Return the measured pulse repetition rate in Hz, for any trigger source."""
        with self._exchange():
            return float(self._laser.send_cmd("rlf?"))

    def get_interlock_open(self) -> bool:
        with self._exchange():
            return self._laser.interlock().strip() == "1"

    def get_fault(self) -> str:
        with self._exchange():
            raw = self._laser.get_fault()
        return _decode_fault(raw)

    def clear_fault(self) -> None:
        with self._exchange():
            self._laser.clear_fault()

    def get_leds(self) -> dict:
        """Status of the 4 status LEDs (manual Section 8.3, command `leds?`)."""
        with self._exchange():
            bits = int(self._laser.send_cmd("leds?"))
        return {
            "power_on": bool(bits & 0b0001),
            "laser_on": bool(bits & 0b0010),
            "laser_lock": bool(bits & 0b0100),
            "error": bool(bits & 0b1000),
        }

    def get_autostart_enabled(self) -> bool:
        with self._exchange():
            return self._laser.send_cmd("@cobas?").strip() == "1"

    def get_serial_number(self) -> str | None:
        return self._laser.serialnumber if self._laser is not None else None

    def get_operating_hours(self) -> float:
        with self._exchange():
            return float(self._laser.get_ophours())

    def get_operating_state(self) -> str:
        """Autostart/operating state via `gom?` — informational display only.

        See the `_OPERATING_STATES` caveat above: this mapping has been
        observed to disagree with reality on the Tor XE, so it is NOT used
        in `is_ready()` or `get_status()["ready"]`.
        """
        with self._exchange():
            code = self._laser.send_cmd("gom?").strip()
        return f"{_OPERATING_STATES.get(code, 'unknown')} (raw {code})"

    def is_ready(self) -> bool:
        """True if the laser has no active fault and the interlock is closed —
        i.e. nothing documented is blocking it from being turned on.

        The manual doesn't document a single "can it turn on" query for the
        Tor XE; this is derived from the interlock and fault state, the two
        documented conditions the manual (Section 9, Troubleshooting) says
        stop emission.
        """
        return not self.get_interlock_open() and self.get_fault().strip().lower() == "no fault"

    def get_status(self) -> dict:
        interlock_open = self.get_interlock_open()
        fault = self.get_fault()
        return {
            "enabled": self.get_enabled(),
            "trigger_source": self.get_trigger_source(),
            "repetition_rate_hz": self.get_repetition_rate(),
            "interlock_open": interlock_open,
            "fault": fault,
            "operating_state": self.get_operating_state(),
            "ready": not interlock_open and fault.strip().lower() == "no fault",
            "autostart_enabled": self.get_autostart_enabled(),
            "leds": self.get_leds(),
            "serial_number": self.get_serial_number(),
            "operating_hours": self.get_operating_hours(),
        }


class MockLaserController:
    """Simulated Tor XE for UI development without hardware."""

    def __init__(self):
        self._enabled = False
        self._trigger_source = "internal"
        self._internal_rate_hz = 1000.0
        self._fault = "no fault"
        self._interlock_open = False
        self._autostart_enabled = True
        self._serial_number = "MOCK-000000"
        self._operating_hours = 0.0
        self._operating_state = "Completed"

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

    def set_trigger_source(self, source: str) -> None:
        if source not in _TRIGGER_SOURCES:
            raise ValueError(f"invalid trigger source: {source!r}")
        self._trigger_source = source

    def get_trigger_source(self) -> str:
        return self._trigger_source

    def set_internal_rate(self, rate_hz: float) -> None:
        self._internal_rate_hz = float(rate_hz)

    def get_internal_rate_setpoint(self) -> float:
        return self._internal_rate_hz

    def get_repetition_rate(self) -> float:
        return self._internal_rate_hz if self._enabled else 0.0

    def get_interlock_open(self) -> bool:
        return self._interlock_open

    def get_fault(self) -> str:
        return self._fault

    def clear_fault(self) -> None:
        self._fault = "no fault"

    def get_leds(self) -> dict:
        return {
            "power_on": True,
            "laser_on": self._enabled,
            "laser_lock": self._enabled,
            "error": self._fault != "no fault",
        }

    def get_autostart_enabled(self) -> bool:
        return self._autostart_enabled

    def get_serial_number(self) -> str | None:
        return self._serial_number

    def get_operating_hours(self) -> float:
        return self._operating_hours

    def get_operating_state(self) -> str:
        return self._operating_state

    def is_ready(self) -> bool:
        return not self._interlock_open and self._fault.strip().lower() == "no fault"

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "trigger_source": self._trigger_source,
            "repetition_rate_hz": self.get_repetition_rate(),
            "interlock_open": self.get_interlock_open(),
            "fault": self.get_fault(),
            "operating_state": self.get_operating_state(),
            "ready": self.is_ready(),
            "autostart_enabled": self.get_autostart_enabled(),
            "leds": self.get_leds(),
            "serial_number": self.get_serial_number(),
            "operating_hours": self.get_operating_hours(),
        }
