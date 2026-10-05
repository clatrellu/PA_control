"""Oscilloscope controllers: Red Pitaya STEM 125-10 and PicoScope 5444D MSO."""
from __future__ import annotations
import logging
import socket
import time
from typing import Any
import numpy as np

_log = logging.getLogger(__name__)

# Linux-only socket option; guarded since this runs cross-platform (the Qt
# app has been run from Windows in this project too).
_HAS_QUICKACK = hasattr(socket, "TCP_QUICKACK")

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

# How capture_block reads the capture window (quick win #3):
#   "tpos"   — ACQ:TPOS? + DATA:STA:N? (two queries, known-good).
#   "verify" — does the "tpos" read, then also reads via DATA:LAT:N? on the
#              same capture and logs whether the two match. Returns the
#              "tpos" data, so the display stays trustworthy while testing.
#   "latest" — ACQ:SOURx:DATA:LAT:N? only (one query). Verified on this
#              firmware at 125 MS/s with no pre-trigger (550/550 exact
#              matches); run "verify" once before relying on it with
#              pre-trigger > 0 or another sample rate.
_READ_MODE = "latest"

# This firmware counts ACQ:TRIG:DLY from the middle of the buffer: verify
# mode measured WPOS-TPOS = DLY + 8191 on every capture (DLY=1250 → 9441).
# The "tpos" read doesn't care where the write pointer stops, but LAT:N?
# does, so the other modes subtract this. LAT:N? also includes the sample
# at WPOS, so stopping at WPOS-TPOS = n_posttrigger put its window one
# sample late (verify: shift=1); hence 8192, stopping one sample earlier.
_DLY_OFFSET = 8192


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
    MAX_SAMPLES = _BUFFER_SIZE  # hardware acquisition buffer depth

    def __init__(self, ip: str = RP_DEFAULT_IP, port: int = 5000, timeout: float = 10.0):
        self._ip = ip
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._channel = "CH1"
        self._coupling = "DC"
        self._range = "1 V"
        # Cache of the (sample_rate_hz, duration_ms, trigger_mv, pretrigger_ms)
        # last sent to the FPGA via _configure_acquisition, so repeat
        # capture_block calls with unchanged params (the common case in a
        # continuous streaming loop) skip the RST/GAIN/DEC/TRIG:LEV/TRIG:DLY
        # round-trips and only pay for arming the trigger and reading data.
        # See capture_block.
        self._armed_params: tuple[float, float, float, float] | None = None
        self._decimation = 1
        self._actual_rate = _RP_CLOCK_HZ
        self._n_samples = 0
        self._n_pretrigger = 0
        # Trigger source channel, independent of the data channel above —
        # e.g. a laser sync pulse wired into CH2 while CH1 is recorded.
        # None means "trigger on the data channel itself" (the old behavior).
        self._trigger_channel: str | None = None
        self._trigger_range = "20 V"
        self._trigger_edge = "rising"  # "rising" or "falling"
        # Set in connect(): whether the firmware answers ACQ:TRIG:FILL? at
        # all. If not, capture_block just uses its fixed settle wait.
        self._has_fill_query = False
        # [matches, total] for _READ_MODE == "verify"; None once verify has
        # been disabled because the board didn't answer.
        self._verify_counts: list[int] | None = [0, 0]

    # ------------------------------------------------------------------
    # SCPI transport
    # ------------------------------------------------------------------

    def _send(self, cmd: str) -> None:
        assert self._sock is not None, "Not connected"
        # TCP_QUICKACK isn't permanent — the kernel lets it lapse after some
        # idle period, and capture_block() has real gaps (the settle wait,
        # waiting for the GUI between captures) that can be long enough for
        # that to happen. Re-applying it once here, right before every send,
        # keeps it fresh regardless of how much idle time preceded this
        # command — a single setsockopt per send, not per chunk of a
        # transfer, so it doesn't have the mid-bulk-transfer downside that
        # ruled out doing this inside _recv() for bulk reads.
        if _HAS_QUICKACK:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
        _log.debug("Red Pitaya <- %s", cmd)
        self._sock.sendall((cmd + "\r\n").encode())

    def _recv(self, bulk: bool = False) -> str:
        """Read one SCPI reply, terminated by \\r\\n.

        bulk selects how TCP_QUICKACK is used, per hardware measurements:
        the Red Pitaya's SCPI server has a ~40ms per-command stall unless we
        force an immediate ACK (delayed ACK interacting with the server's
        own Nagle-serialized send queue, best guess) — re-applying
        TCP_QUICKACK after every chunk fixes this and is ~40-90x faster for
        small, single-chunk replies (bulk=False, the default — status
        queries, TPOS, etc.). But doing that on every chunk of a large,
        multi-chunk reply (a DATA:STA:N? sample read) measured *slower*
        overall than leaving it alone — likely fighting the TCP stack's
        normal flow control for a longer transfer — so bulk=True (used for
        DATA:STA:N?) skips the re-application entirely.
        """
        assert self._sock is not None, "Not connected"
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = self._sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if _HAS_QUICKACK and not bulk:
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
        reply = buf.decode().strip()
        _log.debug("Red Pitaya -> %s", reply[:200])
        return reply

    def _ask(self, cmd: str, bulk: bool = False) -> str:
        self._send(cmd)
        return self._recv(bulk=bulk)

    def _recv_block(self) -> bytes:
        """Read one IEEE-488.2 definite-length binary block: #<n><len><data>\\r\\n.

        Used for binary sample-data replies (ACQ:DATA:FORMAT BIN, confirmed
        against real hardware — see _ask_binary). Must NOT scan for \\r\\n as
        a terminator like _recv() does: binary sample bytes can coincidentally
        contain that exact sequence. The block's declared length is the only
        reliable way to know where the data ends, so this reads exactly that
        many bytes instead. No TCP_QUICKACK re-application here — this is a
        bulk, potentially multi-chunk transfer, and that trick measured
        *slower* for that case (see _recv's docstring).
        """
        assert self._sock is not None, "Not connected"
        buf = b""
        while len(buf) < 2:
            chunk = self._sock.recv(2 - len(buf))
            if not chunk:
                raise ConnectionError("socket closed while reading block header")
            buf += chunk
        if buf[0:1] != b"#":
            raise ValueError(f"expected an IEEE-488.2 block header, got {buf!r}")
        n_len_digits = int(buf[1:2])
        header_len = 2 + n_len_digits
        while len(buf) < header_len:
            chunk = self._sock.recv(header_len - len(buf))
            if not chunk:
                raise ConnectionError("socket closed while reading block length")
            buf += chunk
        data_len = int(buf[2:header_len])
        total_len = header_len + data_len + 2  # +2 for the trailing \r\n
        while len(buf) < total_len:
            chunk = self._sock.recv(min(65536, total_len - len(buf)))
            if not chunk:
                break
            buf += chunk
        _log.debug("Red Pitaya -> <binary block, %d data bytes>", data_len)
        return buf[header_len:header_len + data_len]

    def _ask_binary(self, cmd: str) -> np.ndarray:
        """Ask for sample data in ACQ:DATA:FORMAT BIN mode and parse the
        reply as big-endian float32 — confirmed against real hardware to
        already be in calibrated volts (ACQ:DATA:Units VOLTS), matching the
        ASCII path's values exactly, so no separate calibration math needed.

        Returns an empty array rather than raising on a malformed reply
        (e.g. an "ERR!-3"-style error string instead of a real block, or a
        buffer read racing the acquisition) — same resilience contract the
        old ASCII parser had, so a hiccup drops one frame instead of halting
        continuous acquisition.
        """
        self._send(cmd)
        try:
            payload = self._recv_block()
        except (ValueError, ConnectionError) as exc:
            _log.warning("Red Pitaya: malformed binary data response: %s", exc)
            return np.array([], dtype=float)
        return np.frombuffer(payload, dtype=">f4").astype(float)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Disable Nagle's algorithm: this is a small-request/small-response
        # protocol (every capture is many short SCPI round-trips), which is
        # exactly the pattern that suffers a ~40ms stall per round-trip when
        # Nagle interacts with the server's delayed-ACK timer. Measured on
        # hardware: every command — even a trivial *IDN?/ACQ:TRIG:STAT? — was
        # taking ~40-48ms with this left at its default (enabled).
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(self._timeout)
        self._sock.connect((self._ip, self._port))
        self._send("ACQ:RST")
        self._apply_channel_config()
        self._armed_params = None
        self._has_fill_query = self._probe_fill_query()
        _log.info("Red Pitaya: ACQ:TRIG:FILL? supported: %s", self._has_fill_query)

    def _probe_fill_query(self) -> bool:
        """Check once whether the firmware answers ACQ:TRIG:FILL?.

        Short timeout because this firmware silently drops commands it
        doesn't know instead of replying (seen with ACQ:TRIG:POS?), which
        would otherwise hang until the full socket timeout.
        """
        assert self._sock is not None, "Not connected"
        self._sock.settimeout(0.5)
        try:
            return self._ask("ACQ:TRIG:FILL?") in ("0", "1")
        except socket.timeout:
            return False
        finally:
            self._sock.settimeout(self._timeout)

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

    def configure_trigger_channel(
        self,
        channel: str | None,
        range_label: str = "20 V",
        edge: str = "rising",
    ) -> None:
        """Set which channel the trigger fires on, independent of the data
        channel set via configure_channel(). Pass None (the default) to
        trigger on the data channel itself. Use this when the trigger source
        is physically different from what's being recorded — e.g. a laser
        sync pulse wired into CH2 while the transducer signal on CH1 is
        captured.

        edge selects "rising" (default) or "falling" — use "falling" when
        the source pulses low (dips down) rather than high on the actual
        event, e.g. some lasers' Signal Out drops on firing and recovers
        afterward, so the physically meaningful edge is the fall, not the
        rise back to baseline.
        """
        self._trigger_channel = channel
        self._trigger_range = range_label
        self._trigger_edge = edge
        if self.is_connected:
            self._apply_channel_config()

    def _apply_channel_config(self) -> None:
        # ACQ:RST resets more than just gain — it also reverts the data
        # format/units below back to their ASCII/VOLTS defaults. This method
        # already runs after every RST (from connect() and, via
        # _configure_acquisition(), on every parameter change), so it's the
        # right place to keep BIN format asserted too, the same way gain is.
        self._send("ACQ:DATA:FORMAT BIN")
        self._send("ACQ:DATA:Units VOLTS")

        ch = _CHANNELS[self._channel]
        gain, _ = _RANGES[self._range]
        self._send(f"ACQ:SOUR{ch}:GAIN {gain}")
        if self._trigger_channel and self._trigger_channel != self._channel:
            trig_ch = _CHANNELS[self._trigger_channel]
            trig_gain, _ = _RANGES[self._trigger_range]
            self._send(f"ACQ:SOUR{trig_ch}:GAIN {trig_gain}")

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def _configure_acquisition(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float,
        pretrigger_ms: float,
    ) -> None:
        """Apply decimation, trigger level/delay, and reset — once per parameter set.

        capture_block only calls this when (sample_rate_hz, duration_ms,
        trigger_mv, pretrigger_ms) differ from the last call. Re-sending
        ACQ:RST and the rest on every single acquisition (the previous
        behavior) added several blocking SCPI round-trips of dead time
        *before* the trigger was even armed; with a free-running laser
        trigger, pulses landing in that window were simply never seen, which
        showed up as the signal being sporadically missing rather than
        under-sampled.
        """
        decimation = _rate_to_decimation(sample_rate_hz)
        actual_rate = _RP_CLOCK_HZ / decimation
        n_samples = min(int(actual_rate * duration_ms / 1e3), _BUFFER_SIZE)
        # Split the requested window into pre/post-trigger portions. DLY is
        # the number of samples kept *after* the trigger before acquisition
        # stops — the rest of n_samples comes from before the trigger event,
        # read by starting the buffer read earlier (see capture_block).
        n_pretrigger = min(int(actual_rate * pretrigger_ms / 1e3), n_samples)
        n_posttrigger = n_samples - n_pretrigger
        trig_v = trigger_mv / 1e3

        self._send("ACQ:RST")
        self._apply_channel_config()
        self._send(f"ACQ:DEC {decimation}")
        self._send(f"ACQ:TRIG:LEV {trig_v:.6f}")
        dly = n_posttrigger if _READ_MODE == "tpos" else n_posttrigger - _DLY_OFFSET
        self._send(f"ACQ:TRIG:DLY {dly}")

        self._decimation = decimation
        self._actual_rate = actual_rate
        self._n_samples = n_samples
        self._n_pretrigger = n_pretrigger
        self._armed_params = (sample_rate_hz, duration_ms, trigger_mv, pretrigger_ms)

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
        pretrigger_ms: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Triggered single-block capture.

        pretrigger_ms is how much of duration_ms should come from *before*
        the trigger event (clamped to duration_ms); the rest is post-trigger.
        In the returned time_us, 0 marks the trigger event itself, so
        pre-trigger samples come back with negative timestamps.

        Returns (time_us, voltage_mv) as NumPy arrays.
        """
        params = (sample_rate_hz, duration_ms, trigger_mv, pretrigger_ms)
        if self._armed_params != params:
            self._configure_acquisition(*params)

        n_samples = self._n_samples
        n_pretrigger = self._n_pretrigger
        actual_rate = self._actual_rate
        ch = _CHANNELS[self._channel]
        trig_ch = _CHANNELS[self._trigger_channel] if self._trigger_channel else ch

        t_arm_start = time.monotonic()
        self._send("ACQ:START")

        if trigger_mv == 0.0:
            # Immediate capture: force the trigger right away.
            self._send("ACQ:TRIG NOW")
            deadline = time.monotonic() + 2.0
            # No sleep between polls: _ask() already blocks on recv() waiting
            # for the reply, so this doesn't busy-spin — a sleep here is just
            # added latency on top of each already-blocking round-trip.
            while time.monotonic() < deadline:
                if self._ask("ACQ:TRIG:STAT?") == "TD":
                    break
        else:
            # Wait for a genuine edge (rising or falling, per
            # self._trigger_edge) at the requested threshold (up to 5 s) —
            # "Normal" trigger mode. Unlike the old behavior, a timeout here
            # does NOT force ACQ:TRIG NOW: forcing a capture off a real
            # trigger source fabricates a frame from whatever noise happens to
            # be in the buffer, which is "Auto" trigger mode and was the
            # actual cause of the sporadic-signal symptom — on cycles where
            # the real transducer pulse didn't cross the threshold, the forced
            # capture returned noise that looked like a missing signal. On
            # timeout we just report no data for this cycle so the caller can
            # hold the last real trace instead (see server.py's _capture_loop).
            edge_suffix = "PE" if self._trigger_edge == "rising" else "NE"
            self._send(f"ACQ:TRIG CH{trig_ch}_{edge_suffix}")
            deadline = time.monotonic() + 5.0
            # No sleep between polls — see the note in the trigger_mv == 0.0
            # branch above.
            while time.monotonic() < deadline:
                if self._ask("ACQ:TRIG:STAT?") == "TD":
                    break
            else:
                return np.array([]), np.array([])

        t_triggered = time.monotonic()
        n_posttrigger = n_samples - n_pretrigger
        if _READ_MODE != "tpos":
            # LAT:N? reads backwards from the write pointer, so unlike the
            # TPOS read it needs the acquisition to have actually stopped
            # (n_posttrigger samples after the trigger) — otherwise the
            # window slides. FILL? ("buffer is full") can't confirm that, so
            # wait out the post-trigger span instead. The trigger happened
            # before we saw TD, so timing from here is conservative; for
            # short windows this is microseconds.
            remaining_s = t_triggered + n_posttrigger / actual_rate + 0.0005 - time.monotonic()
            if remaining_s > 0:
                time.sleep(remaining_s)
        # Runs in every mode, including "latest": "latest" without it (only
        # the short wait above) showed false triggers — >100 Hz with the
        # laser at 100 Hz, and captures with the laser off — while "verify",
        # which had it, didn't. The Red Pitaya OS 2.x example also waits for
        # FILL? after TD before reading.
        #
        # The fixed wait below is the one the code has always used, known
        # to work on this firmware. FILL? may only shorten it, never lengthen it or drop
        # the frame: per the Red Pitaya docs FILL? means "buffer is full",
        # which may never become 1 in some setups (seen once, with
        # the trigger on the recorded channel itself), so if it hasn't
        # reported 1 by the end of this wait we read anyway, as before.
        settle_deadline = t_triggered + n_samples / actual_rate + 0.005
        filled = False
        if self._has_fill_query:
            while time.monotonic() < settle_deadline:
                if self._ask("ACQ:TRIG:FILL?") == "1":
                    filled = True
                    break
        if not filled:
            remaining_s = settle_deadline - time.monotonic()
            if remaining_s > 0:
                time.sleep(remaining_s)
        t_settled = time.monotonic()

        if _READ_MODE == "latest":
            voltage_v = self._ask_binary(f"ACQ:SOUR{ch}:DATA:LAT:N? {n_samples}")
        else:
            # ACQ:TRIG:POS? goes unanswered on current firmware (REDPITAYA,
            # INSTR2025,,01-21) — confirmed by probing the SCPI server
            # directly, it silently drops the command instead of replying,
            # which hung this read until the socket timeout. ACQ:TPOS? is the
            # live equivalent. Still guarded by the fallback in case of a
            # malformed reply.
            try:
                trig_pos = int(self._ask("ACQ:TPOS?"))
            except ValueError:
                trig_pos = 0

            # Start reading n_pretrigger samples before the trigger position
            # (wrapping through address 0 via modulo, same circular buffer
            # _read_samples already handles wrapping forward past the end of).
            start_pos = (trig_pos - n_pretrigger) % _BUFFER_SIZE
            voltage_v = self._read_samples(ch, start_pos, n_samples)
            if _READ_MODE == "verify":
                self._verify_latest_read(ch, trig_pos, n_samples, n_posttrigger, voltage_v)
        t_read = time.monotonic()

        _log.debug(
            "capture_block timing: arm+trigger-wait=%.1fms  settle-sleep=%.1fms  "
            "data-read=%.1fms  (n_samples=%d, mode=%s)",
            (t_triggered - t_arm_start) * 1e3,
            (t_settled - t_triggered) * 1e3,
            (t_read - t_settled) * 1e3,
            n_samples,
            _READ_MODE,
        )

        n = len(voltage_v)
        # Index n_pretrigger is the trigger event itself — t=0 — so
        # pre-trigger samples come back with negative timestamps.
        time_us = (np.arange(n) - n_pretrigger) / actual_rate * 1e6
        return time_us, voltage_v * 1e3

    def _verify_latest_read(
        self,
        ch: int,
        trig_pos: int,
        n_samples: int,
        n_posttrigger: int,
        reference: np.ndarray,
    ) -> None:
        """Read the same capture again via DATA:LAT:N? and log whether it
        matches the known-good TPOS read. Also logs WPOS-TPOS: LAT:N? is
        only correct if the write pointer stops at the last sample of the
        window, n_posttrigger - 1 samples after the trigger (see _DLY_OFFSET).
        """
        expected_offset = n_posttrigger - 1
        if self._verify_counts is None:
            return
        assert self._sock is not None, "Not connected"
        # Short timeout: this firmware silently drops commands it doesn't
        # know (seen with ACQ:TRIG:POS?), which would otherwise stall for the
        # full socket timeout and kill the acquisition.
        self._sock.settimeout(1.0)
        try:
            latest = self._ask_binary(f"ACQ:SOUR{ch}:DATA:LAT:N? {n_samples}")
            try:
                wpos_offset = (int(self._ask("ACQ:WPOS?")) - trig_pos) % _BUFFER_SIZE
            except ValueError:
                wpos_offset = None
        except socket.timeout:
            _log.warning(
                "LAT:N? verify: no reply from the board — firmware likely doesn't "
                "support DATA:LAT:N? (or WPOS?). Verify disabled; keep _READ_MODE = 'tpos'."
            )
            self._verify_counts = None
            return
        finally:
            self._sock.settimeout(self._timeout)
        match = latest.shape == reference.shape and np.array_equal(latest, reference)
        self._verify_counts[1] += 1
        if match:
            self._verify_counts[0] += 1
        elif self._verify_counts[1] - self._verify_counts[0] <= 10:
            max_diff = (
                float(np.max(np.abs(latest - reference)))
                if latest.shape == reference.shape and len(latest) else float("nan")
            )
            _log.warning(
                "LAT:N? verify MISMATCH: len %d vs %d, max |diff|=%.4g V, "
                "WPOS-TPOS=%s (expected %d), shift=%s",
                len(latest), len(reference), max_diff, wpos_offset, expected_offset,
                self._find_shift(latest, reference),
            )
        matches, total = self._verify_counts
        if total == 1 or total % 50 == 0:
            _log.info(
                "LAT:N? verify: %d/%d captures matched (last WPOS-TPOS=%s, expected %d)",
                matches, total, wpos_offset, expected_offset,
            )

    @staticmethod
    def _find_shift(latest: np.ndarray, reference: np.ndarray, max_shift: int = 32) -> int | None:
        """Return k such that latest[i] == reference[i + k] over the overlap
        (positive k: the LAT:N? window sits later than the TPOS window), or
        None if no shift within ±max_shift gives an exact match.
        """
        n = min(len(latest), len(reference))
        for k in sorted(range(-max_shift, max_shift + 1), key=abs):
            if n - abs(k) < 16:
                continue
            if k >= 0:
                a, b = latest[:n - k], reference[k:n]
            else:
                a, b = latest[-k:n], reference[:n + k]
            if np.array_equal(a, b):
                return k
        return None

    def _read_samples(self, ch: int, trig_pos: int, n_samples: int) -> np.ndarray:
        """Read n_samples from the circular buffer starting at trig_pos, handling wrap-around."""
        if trig_pos + n_samples <= _BUFFER_SIZE:
            return self._ask_binary(f"ACQ:SOUR{ch}:DATA:STA:N? {trig_pos},{n_samples}")
        n1 = _BUFFER_SIZE - trig_pos
        n2 = n_samples - n1
        r1 = self._ask_binary(f"ACQ:SOUR{ch}:DATA:STA:N? {trig_pos},{n1}")
        r2 = self._ask_binary(f"ACQ:SOUR{ch}:DATA:STA:N? 0,{n2}")
        return np.concatenate([r1, r2])


class MockOscilloscopeController:
    """Simulated Red Pitaya — generates a realistic damped-sinusoid PA signal."""

    CHANNEL_LABELS = CHANNEL_LABELS
    COUPLING_LABELS = COUPLING_LABELS
    RANGE_LABELS = RANGE_LABELS
    SAMPLE_RATES = RP_SAMPLE_RATES
    MAX_SAMPLES = _BUFFER_SIZE

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

    def configure_trigger_channel(
        self, channel: str | None, range_label: str = "20 V", edge: str = "rising"
    ) -> None:
        pass  # simulated capture ignores trigger source entirely

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
        pretrigger_ms: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        n = int(sample_rate_hz * duration_ms / 1e3)
        pretrigger_ms = min(pretrigger_ms, duration_ms)
        t_us = np.linspace(-pretrigger_ms * 1e3, (duration_ms - pretrigger_ms) * 1e3, n)

        # ~10 MHz damped sinusoid (photoacoustic-like) + Gaussian noise.
        # Only post-trigger (t >= 0) carries the simulated pulse — before the
        # trigger there's nothing but baseline noise, same as a real capture.
        freq_hz = 10e6
        decay_us = duration_ms * 100
        t_post = np.clip(t_us, 0, None)
        signal = (
            120.0
            * np.exp(-t_post / decay_us)
            * np.sin(2 * np.pi * freq_hz * t_post / 1e6)
            * (t_us >= 0)
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

# The 5444D's actual buffer depth depends on resolution/timebase (queried live
# via ps5000aGetTimebase2 in capture_block); this is just a sane upper bound
# used both as a safety cap there and as the advertised MAX_SAMPLES below.
_PICO_MAX_SAMPLES: int = 10_000_000

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
    MAX_SAMPLES = _PICO_MAX_SAMPLES

    def __init__(self) -> None:
        import ctypes
        self._handle = ctypes.c_int16(0)
        self._channel = "A"
        self._coupling = "DC"
        self._range = "1 V"
        self._trigger_edge = "rising"  # "rising" or "falling"
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

    def configure_trigger_channel(
        self,
        channel: str | None,
        range_label: str = "20 V",
        edge: str = "rising",
    ) -> None:
        """Set the channel the trigger fires on, independent of the data
        channel — see OscilloscopeController.configure_trigger_channel.

        Only channel=None (trigger on the data channel itself, the default)
        is supported here: a genuinely separate trigger channel needs that
        channel enabled in _apply_channel_config's ps5000aSetChannel loop and
        its own range passed into ps5000aSetSimpleTrigger's threshold
        conversion, and this hasn't been exercised against real 5444D
        hardware, so it's refused rather than silently doing the wrong thing.

        edge ("rising" or "falling") is supported regardless — it's just the
        ps5000aSetSimpleTrigger direction argument, independent of channel.
        """
        if channel is not None and channel != self._channel:
            raise NotImplementedError(
                "PicoScope5444DController: a trigger channel separate from "
                "the data channel isn't implemented yet"
            )
        self._trigger_edge = edge

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
        pretrigger_ms: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Triggered single-block capture.

        pretrigger_ms is how much of duration_ms should come from *before*
        the trigger event (clamped to duration_ms); the rest is post-trigger.
        In the returned time_us, 0 marks the trigger event itself, so
        pre-trigger samples come back with negative timestamps.

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
            _PICO_MAX_SAMPLES,
        )
        n_pretrigger = min(int(actual_rate_hz * pretrigger_ms / 1e3), n_samples)
        n_posttrigger = n_samples - n_pretrigger

        ch_idx = _PICO_CHANNELS[self._channel]
        range_idx, _ = _PICO_RANGES[self._range]
        maxADC = ctypes.c_int16(_PICO_MAX_ADC)

        # Configure trigger (auto-trigger after 1 s)
        threshold_adc = mV2adc(trigger_mv, range_idx, maxADC)
        direction = 2 if self._trigger_edge == "rising" else 3  # PS5000A_RISING / _FALLING
        status = self._ps.ps5000aSetSimpleTrigger(
            self._handle,
            1,               # enable
            ch_idx,          # source
            threshold_adc,   # threshold in ADC counts
            direction,
            0,               # delay
            1000,            # autoTrigger_ms
        )
        assert_pico_ok(status)

        # Arm and wait for block capture
        time_indisposed = ctypes.c_int32()
        status = self._ps.ps5000aRunBlock(
            self._handle, n_pretrigger, n_posttrigger, timebase,
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
        # Index n_pretrigger is the trigger event itself — t=0 — so
        # pre-trigger samples come back with negative timestamps.
        time_us = (np.arange(n) - n_pretrigger) / actual_rate_hz * 1e6
        return time_us, voltage_mv


class MockPicoScope5444DController:
    """Simulated PicoScope 5444D — generates a realistic damped-sinusoid PA signal."""

    CHANNEL_LABELS = PICO_CHANNEL_LABELS
    COUPLING_LABELS = PICO_COUPLING_LABELS
    RANGE_LABELS = PICO_RANGE_LABELS
    SAMPLE_RATES = PICO_SAMPLE_RATES
    MAX_SAMPLES = _PICO_MAX_SAMPLES

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

    def configure_trigger_channel(
        self, channel: str | None, range_label: str = "20 V", edge: str = "rising"
    ) -> None:
        pass  # simulated capture ignores trigger source entirely

    def capture_block(
        self,
        sample_rate_hz: float,
        duration_ms: float,
        trigger_mv: float = 0.0,
        pretrigger_ms: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        n = int(sample_rate_hz * duration_ms / 1e3)
        pretrigger_ms = min(pretrigger_ms, duration_ms)
        t_us = np.linspace(-pretrigger_ms * 1e3, (duration_ms - pretrigger_ms) * 1e3, n)

        freq_hz = 10e6
        decay_us = duration_ms * 100
        t_post = np.clip(t_us, 0, None)
        signal = (
            120.0
            * np.exp(-t_post / decay_us)
            * np.sin(2 * np.pi * freq_hz * t_post / 1e6)
            * (t_us >= 0)
        )
        noise = self._rng.normal(0, 8, n)
        return t_us, signal + noise
