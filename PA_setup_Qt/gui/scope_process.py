"""Continuous Red Pitaya acquisition in a separate process.

In the GUI process the acquisition thread shares Python's GIL with the GUI
thread, and waiting for it ~20 times per capture (once per SCPI reply) made
the loop miss shots: 50–60 Hz with the laser at 100 Hz, while the same
capture loop headless caught all 100. A child process has its own
interpreter and GIL, so the GUI can't delay it.
"""
from __future__ import annotations

import queue
import time

from pa_hardware.oscilloscope import OscilloscopeController


def acquisition_main(config: dict, params: dict, frames, stop) -> None:
    """Child-process entry point: connect, capture until `stop` is set.

    Puts ("frame", time_us, voltage_mv, captured_at) per triggered capture,
    ("error", message) on failure, and always ("done",) last. captured_at
    is time.monotonic(), which is system-wide on Linux, so the GUI can
    compute the acquisition rate from it regardless of queue delays.
    """
    scope = OscilloscopeController.from_config(config)
    try:
        scope.connect()
        while not stop.is_set():
            t, v = scope.capture_block(**params)
            if len(v):
                frames.put(("frame", t, v, time.monotonic()))
    except Exception as exc:
        frames.put(("error", str(exc)))
    finally:
        scope.disconnect()
        frames.put(("done",))


def drain(frames, timeout_s: float):
    """Yield queued messages, waiting up to timeout_s for the first one."""
    try:
        yield frames.get(timeout=timeout_s)
        while True:
            yield frames.get_nowait()
    except queue.Empty:
        return
