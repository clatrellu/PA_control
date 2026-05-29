# Architecture & Stack Decisions

## Project overview

Two parallel GUI implementations for controlling a photoacoustic (PA) microscopy-type
setup: Cobolt laser, Thorlabs galvanometric mirrors, PicoScope 5000 acoustic detector,
and (planned) a pulse generator.

---

## Decision 1 — Framework architecture: custom vs ImSwitch

**Considered:** ImSwitch (SciLifeLab microscopy control framework)

**Chosen:** Custom — PyQt6 GUI + thin hardware abstraction layer

**Reasons:**
- ImSwitch assumes a microscope mental model (camera-like detectors). An oscilloscope
  returning time-series waveforms is a poor fit; fighting the abstraction costs more
  than the framework provides.
- For a 1–3 person lab setup, the ImSwitch architecture (managers / controllers /
  widgets + REST bus) is significant upfront investment for marginal gain.
- Custom code stays readable and fully owned. Adding a pulse generator is one new
  class + one new widget, with no framework contracts to satisfy.
- ImSwitch pays off in large shared-lab contexts where many instrument types are
  hot-swapped by different users.

**Tradeoff accepted:** We own all the scaffolding (threading, error handling, layout).

---

## Decision 2 — Desktop GUI: PyQt6

**Considered:** PySide6, tkinter, wxPython, Dear PyGui

**Chosen:** PyQt6

**Reasons:**
- Mature, well-documented, large community.
- pyqtgraph (oscilloscope plot) integrates natively and uses OpenGL acceleration.
- Strong threading model (QThread + signals/slots) keeps hardware I/O off the main thread.
- Ships as a single process — no server, no browser, no network stack.
- Fast for real-time waveform display: pyqtgraph with `setData()` easily sustains >30 Hz
  on 125K-point traces without GPU.

**Tradeoff accepted:** PyQt6 has GPL/commercial dual license (LGPL for PyQt6 specifically
is fine for research use, but worth noting).

---

## Decision 3 — Web frontend: FastAPI + vanilla JS + uPlot

**Considered:** NiceGUI, Dash, Streamlit, React + FastAPI, Svelte + FastAPI

**Chosen:** FastAPI (Python) + vanilla JS + uPlot (JS charting)

**Reasons:**

*FastAPI vs Flask/Django:*
- Native async (ASGI), handles concurrent WebSocket + REST without threads.
- `asyncio.to_thread()` offloads blocking hardware I/O to a thread pool cleanly.
- Pydantic models give automatic request validation with zero boilerplate.
- `uvicorn[standard]` bundles `uvloop` (Linux/Mac) for ~2× faster event loop than
  the default asyncio implementation.

*Vanilla JS vs React/Svelte:*
- No build step (npm, webpack, bundler) — the frontend is static files served by
  FastAPI's StaticFiles. Zero toolchain to maintain.
- For an instrument GUI that isn't "reactive" in the SPA sense, framework overhead
  adds complexity without benefit.
- All modern browsers support ES2020 natively; no transpilation needed.

*uPlot vs Plotly / Chart.js / ECharts:*
- uPlot is the fastest JS time-series chart library by a large margin.
  Benchmark: 125K points, 60 Hz update — uPlot renders in <2 ms vs 15–40 ms for
  Plotly. This matters for continuous oscilloscope display.
- Canvas-based (no DOM manipulation per point), specifically designed for
  oscilloscope / telemetry use cases. Used in Grafana.
- MIT licensed, < 50 KB minified.

*Binary WebSocket frames:*
- Waveform data (time_us + voltage_mv) is transmitted as Float32Array binary frames,
  not JSON.
- Frame layout: `[uint32 n_samples][float32 × n time_us][float32 × n voltage_mv]`
- 125K samples = ~1 MB binary vs ~3.5 MB JSON — 3.5× smaller, no JSON parse overhead.
- On the JS side, `Float32Array` views into the ArrayBuffer are zero-copy.

**Tradeoff accepted:** Binary protocol is less debuggable than JSON (no curl-readable
response). The JSON error fallback (`ws.send_json({"error": ...})`) handles exceptions.

---

## Decision 4 — Hardware abstraction: Real + Mock controllers

**Pattern:** Each instrument has two classes, e.g. `LaserController` and
`MockLaserController`, with identical public APIs.

**Reasons:**
- `--mock` flag lets the full GUI run without hardware connected (essential for
  development, demos, and offline configuration).
- Mock controllers generate realistic synthetic data: the mock oscilloscope produces
  a damped 10 MHz sinusoid + Gaussian noise, mimicking a PA signal envelope.
- No conditional logic in the GUI code — it never knows whether it's talking to real
  or mock hardware.

---

## Decision 5 — Instrument libraries

| Instrument | Library | Reason |
|---|---|---|
| Cobolt laser | `pyserial` (direct) | Simple ASCII serial protocol; direct is clearer than wrapping pymeasure's driver. Commands: `l1`/`l0` (enable), `@cobasrp <mW>` (set power), `p?` (read power). Verify against your model's manual. |
| Thorlabs galvos | `nidaqmx` (NI official) | Galvos are analog-controlled (±10 V). Need a NI-DAQ card as intermediary. nidaqmx is the official Python wrapper. |
| PicoScope 5000 | `picosdk` (Pico official) | Official Python wrappers from Pico Technology, wraps their ps5000a C library. Requires PicoScope drivers installed from picotech.com. |

**Note on galvos:** Analog control means the NI-DAQ card is the real hardware interface.
Channel names (e.g. `Dev1/ao0`) are configurable in the connection panel at runtime.

---

## Decision 6 — Project layout per repo

```
PA_setup/        ← PyQt6 desktop app
  main.py        ← uv run python main.py [--mock]
  hardware/      ← instrument abstraction (shared logic)
  gui/           ← PyQt6 widgets

PA_setup_web/    ← FastAPI + browser app  
  run.py         ← uv run python run.py [--mock]  (opens browser automatically)
  server.py      ← FastAPI app, REST + WebSocket
  hardware/      ← same abstraction, copied
  frontend/      ← index.html + app.js + style.css (static files)
```

Both repos use `uv` for dependency management (isolated `.venv` per project, lock file
for reproducibility). No conda, no global pip installs.

---

---

## Decision 7 — Laser trigger frequency modulation

**What it does:** A NI-DAQ counter output channel generates a periodic TTL signal
at a configurable repetition rate. That signal is wired to the Cobolt laser's external
modulation input. The laser fires once per TTL rising edge instead of running CW.

**Implementation:**
- `hardware/trigger.py` — `TriggerController` (nidaqmx counter task) + `MockTriggerController`
- `gui/trigger_widget.py` (Qt) — freq spinbox, presets (100 Hz / 1 kHz / 10 kHz / 50 kHz),
  duty cycle, live pulse-width readout, Start/Stop
- `LaserController.set_modulation_mode('cw' | 'external')` — sends `@cobasd 0/1` over serial
- Web: REST endpoints `/api/trigger/*` + `/api/laser/mode`; laser mode selector in sidebar

**Why NI-DAQ counter output (not software-timed digital):**
- Counter output (`co_channels`) is hardware-timed: the NI-DAQ generates the clock in
  firmware, independent of the PC. Jitter is <10 ns.
- Software-timed digital output (toggling a DO line from Python) has OS-scheduling
  jitter of 1–10 ms — completely unusable for PA where timing accuracy matters.

**Cobolt command for external modulation:**
- `@cobasd 1` — digital (external) modulation mode
- `@cobasd 0` — back to CW
- This is for 08-series; verify against your exact model manual.

**Typical workflow:**
1. Connect trigger (pick counter channel, e.g. `Dev1/ctr0`)
2. Set frequency and duty cycle
3. Click Start — TTL pulses begin immediately
4. Switch laser Mode → "External trigger"
5. Enable laser emission — it now fires at the set rep rate
6. Run oscilloscope acquisition (the acoustic signal appears at the set rate)

**Pulse width:** at 1 kHz, 5 % duty cycle = 50 µs pulse width (shown live in UI).
Keep duty cycle low (≤10 %) for clean laser triggering and to avoid heating effects.

---

---

## Decision 8 — Shared backend: `pa_hardware` package

**Problem:** The hardware abstraction layer was duplicated in `PA_setup/hardware/` and
`PA_setup_web/hardware/`. Any fix or addition had to be applied in two places.

**Solution:** Extracted into a standalone Python package `pa_hardware/` at the repos level,
installed as an editable path dependency in both frontends.

**Final structure:**
```
repos/
├── pa_hardware/              ← single source of truth for all hardware code
│   ├── pyproject.toml        ← declares: numpy, pyserial, nidaqmx, picosdk
│   └── src/pa_hardware/
│       ├── __init__.py
│       ├── laser.py
│       ├── galvo.py
│       ├── oscilloscope.py
│       └── trigger.py
├── PA_setup/                 ← Qt desktop frontend
│   └── pyproject.toml        ← depends on: pa-hardware (path), PyQt6, pyqtgraph
└── PA_setup_web/             ← FastAPI web frontend
    └── pyproject.toml        ← depends on: pa-hardware (path), fastapi, uvicorn
```

**How the path dependency works (uv):**
```toml
# PA_setup/pyproject.toml and PA_setup_web/pyproject.toml
dependencies = ["pa-hardware", ...]

[tool.uv.sources]
pa-hardware = { path = "../pa_hardware", editable = true }
```
`uv sync` builds `pa_hardware` as an editable install into each project's `.venv`.
Changes to `src/pa_hardware/` are immediately visible to both frontends with no
reinstall needed (editable = symlinked, not copied).

**Why not a uv workspace?**
A workspace shares a single `.venv` and lock file across all members. Good for monorepos
where all packages always release together. Here the frontends have incompatible deps
(PyQt6 vs FastAPI) and may be deployed independently, so separate venvs per project
are cleaner. Path deps give the shared-code benefit without the workspace coupling.

**Why not symlinks?**
Symlinks break on Windows and are invisible to `uv`/pip for dependency resolution.
A proper package is portable and explicit.

**Import change (trivial):** `from hardware import ...` → `from pa_hardware import ...`
Only 3 lines changed across the two frontends.

---

## Open questions / future decisions

- [ ] Pulse generator control: which model? USB/GPIB/serial? Decide library then.
- [ ] Shared hardware layer: currently duplicated across repos. Could extract to a
      local uv package if they diverge.
- [ ] Network access: `run.py --host 0.0.0.0` exposes the web GUI on the LAN.
      No auth currently — fine for a closed lab network, not for internet exposure.
- [ ] Data storage: saving individual CSVs. Consider HDF5 (h5py) if scan datasets
      get large.
