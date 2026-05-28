# PA Control

Control software for a photoacoustic scanning setup. Two frontends share a single hardware abstraction backend.

## Hardware

| Instrument | Model | Interface |
|---|---|---|
| Laser | Cobolt 08-series | USB-serial (115200 baud) |
| Galvo mirrors | Thorlabs | NI-DAQ analog output |
| Oscilloscope | PicoScope 5000 | USB (picosdk) |
| Trigger | NI-DAQ counter output | TTL to laser modulation input |

## Repository layout

```
PA_control/
├── pa_hardware/          # shared hardware abstraction layer (editable package)
│   └── src/pa_hardware/
│       ├── laser.py      # Cobolt laser (serial)
│       ├── galvo.py      # Thorlabs galvo (NI-DAQ AO)
│       ├── oscilloscope.py  # PicoScope 5000
│       └── trigger.py    # NI-DAQ counter output for laser repetition rate
├── PA_setup_Qt/          # desktop frontend — PyQt6
└── PA_setup_web/         # browser frontend — FastAPI + vanilla JS
```

Each frontend has its own `.venv` managed by `uv` and depends on `pa_hardware` as a local editable install.

## Running

Both frontends support `--mock` to run without any hardware connected.

**Desktop (PyQt6)**
```bash
cd PA_setup_Qt
uv run python main.py --mock
```

**Web (FastAPI + browser)**
```bash
cd PA_setup_web
uv run python run.py --mock
# opens http://127.0.0.1:8000 automatically
```

For network access from another machine:
```bash
uv run python run.py --host 0.0.0.0 --port 8000
```

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd PA_setup_Qt && uv sync
cd ../PA_setup_web && uv sync
```

`uv sync` creates the `.venv` and installs all dependencies including the `pa_hardware` package.

## Trigger wiring

The NI-DAQ counter output generates a TTL pulse train that drives the laser in external modulation mode. Before starting the trigger, set the laser to external modulation mode from the UI (or via `LaserController.set_modulation_mode('external')`). The counter output terminal depends on your NI-DAQ model — check NI-MAX (e.g. PFI12 on USB-6211, PFI4 on USB-6361).
