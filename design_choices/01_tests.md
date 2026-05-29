# Test Log

Tests run during development to validate the implementations.
Each entry records what was tested, how, and what was observed.

---

## 2026-05-28 — PA_setup (PyQt6)

### Test 1 — Python syntax check (all files)
```
uv run python -c "
import ast, pathlib, sys
for f in pathlib.Path('.').rglob('*.py'):
    ast.parse(f.read_text())
print('All files parsed OK')
"
```
**Result:** `All files parsed OK` ✓

### Test 2 — Mock hardware layer smoke test
```python
laser = MockLaserController()
laser.connect('COM3')
laser.set_power(50.0)
laser.set_enabled(True)
assert laser.get_power() == 50.0
assert laser.get_enabled() is True

galvo = MockGalvoController()
galvo.connect('Dev1/ao0', 'Dev1/ao1')
galvo.move_to(3.5, -2.1)
assert galvo.get_position() == (3.5, -2.1)

scope = MockOscilloscopeController()
scope.connect()
t, v = scope.capture_block(sample_rate_hz=125e6, duration_ms=1.0)
assert len(t) == 125000
assert t[-1] > 900   # ~1000 µs
```
**Result:** All assertions passed ✓

### Test 3 — GUI instantiation (offscreen, no display required)
```
QT_QPA_PLATFORM=offscreen uv run python -c "
...
w = MainWindow(mock=True)
w.show()
print('GUI created OK')
"
```
**Result:** `GUI created OK` ✓

---

## 2026-05-28 — PA_setup_web (FastAPI)

### Test 1 — Python syntax check (all files)
Same as above, run in PA_setup_web directory.
**Result:** `Python syntax OK` ✓

### Test 2 — Hardware layer smoke test
Same assertions as PA_setup Test 2 — hardware layer is identical (copied).
**Result:** `Hardware layer OK` ✓

### Test 3 — FastAPI app import + route registration
```python
from server import app
routes = [r.path for r in app.routes]
expected = ['/api/laser/connect', '/api/galvo/move',
            '/api/scope/connect', '/ws/scope', '/']
for p in expected:
    assert p in routes
```
**Result:** All expected routes present ✓

---

---

## 2026-05-28 — Trigger module (both repos)

### Test 1 — Mock trigger smoke test (PA_setup)
```python
from hardware import MockLaserController, MockTriggerController

laser = MockLaserController()
laser.connect('COM3')
laser.set_modulation_mode('external')
assert laser.get_modulation_mode() == 'external'
laser.set_modulation_mode('cw')
assert laser.get_modulation_mode() == 'cw'

trig = MockTriggerController()
trig.connect('Dev1/ctr0')
assert trig.is_connected
trig.start(1000.0, 0.05)
assert trig.is_running
assert trig.get_status()['freq_hz'] == 1000.0
trig.stop()
assert not trig.is_running
```
**Result:** All assertions passed ✓

### Test 2 — GUI with trigger (PA_setup, offscreen)
```python
w = MainWindow(mock=True)   # includes TriggerWidget
```
**Result:** `PA_setup OK` ✓

### Test 3 — Trigger API routes registered (PA_setup_web)
```python
routes = [r.path for r in app.routes]
for p in ['/api/trigger/connect', '/api/trigger/start',
          '/api/trigger/stop', '/api/laser/mode']:
    assert p in routes
```
**Result:** All routes present: `/api/laser/mode`, `/api/trigger/connect`,
`/api/trigger/disconnect`, `/api/trigger/start`, `/api/trigger/stop`,
`/api/trigger/status` ✓

---

---

## 2026-05-28 — Shared pa_hardware package

### Test 1 — pa_hardware importable from both frontends
```python
# In PA_setup venv:
from pa_hardware import (LaserController, MockLaserController, ...)
from pa_hardware.oscilloscope import RANGE_LABELS, CHANNEL_LABELS, COUPLING_LABELS
```
**Result:** Import OK in both PA_setup and PA_setup_web ✓

### Test 2 — uv sync resolves editable path dep
```
uv sync  # in PA_setup/
#  + pa-hardware==0.1.0 (from file:///…/pa_hardware)  ✓

uv sync  # in PA_setup_web/
#  + pa-hardware==0.1.0 (from file:///…/pa_hardware)  ✓
```

### Test 3 — Qt GUI still launches
```python
w = MainWindow(mock=True)   # imports from pa_hardware, not hardware
```
**Result:** `Qt GUI OK` ✓

### Test 4 — FastAPI routes still registered
All 25 routes confirmed present including `/api/trigger/*` and `/api/laser/mode` ✓

---

## Tests NOT yet run (require hardware)

- [ ] Real Cobolt laser: connect, set power, enable, read back actual power
- [ ] Real NI-DAQ galvo: connect, move X/Y to known voltage, verify mirror deflection
- [ ] Real PicoScope: connect, block capture, verify waveform at known signal
- [ ] WebSocket continuous streaming: measure actual frame rate at 125 MS/s, 1 ms duration
- [ ] Binary frame integrity: compare decoded JS Float32Array vs Python numpy array
      for the same capture (end-to-end test)
- [ ] Save CSV: capture trace, save, re-load in Python/numpy, check data integrity
- [ ] PA_setup_web on a second machine: access via LAN with --host 0.0.0.0

---

## Performance benchmarks (planned)

- Qt/pyqtgraph continuous display: measure actual FPS with mock at 125 MS/s, 1 ms window
- Web/uPlot continuous display: same, measure time from WS binary frame receipt to
  canvas repaint (use `performance.now()` in JS)
- Expected: both capable of >10 Hz continuous update; Qt slightly lower latency
  (in-process), web adds ~1–3 ms WS round-trip
