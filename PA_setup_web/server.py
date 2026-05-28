"""FastAPI backend — serves REST API + WebSocket scope stream + static frontend."""
from __future__ import annotations
import asyncio
import os
import struct
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pa_hardware import (
    LaserController, MockLaserController,
    GalvoController, MockGalvoController,
    OscilloscopeController, MockOscilloscopeController,
    TriggerController, MockTriggerController,
)

FRONTEND = Path(__file__).parent / "frontend"

# ---------------------------------------------------------------------------
# Hardware state — single-process, single-user lab app
# ---------------------------------------------------------------------------
_laser   = None
_galvo   = None
_scope   = None
_trigger = None
_mock    = False


def _init_hardware(mock: bool) -> None:
    global _laser, _galvo, _scope, _trigger, _mock
    _mock    = mock
    _laser   = MockLaserController()   if mock else LaserController()
    _galvo   = MockGalvoController()   if mock else GalvoController()
    _scope   = MockOscilloscopeController() if mock else OscilloscopeController()
    _trigger = MockTriggerController() if mock else TriggerController()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_hardware(mock=os.environ.get("PA_MOCK") == "1")
    yield
    for dev in (_trigger, _laser, _galvo, _scope):
        try:
            dev.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="PA Setup Control", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LaserConnectReq(BaseModel):
    port: str

class LaserPowerReq(BaseModel):
    power_mw: float

class LaserEnableReq(BaseModel):
    enabled: bool

class GalvoConnectReq(BaseModel):
    x_channel: str
    y_channel: str

class GalvoMoveReq(BaseModel):
    x_v: float
    y_v: float

class ScopeConfigReq(BaseModel):
    channel: str = "A"
    coupling: str = "DC"
    range_label: str = "500 mV"

class TriggerConnectReq(BaseModel):
    channel: str = "Dev1/ctr0"

class TriggerStartReq(BaseModel):
    freq_hz: float = 1000.0
    duty_cycle: float = 0.05

class LaserModeReq(BaseModel):
    mode: str   # 'cw' or 'external'


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------
@app.get("/api/info")
async def info():
    return {"mock": _mock}


# ---------------------------------------------------------------------------
# Laser
# ---------------------------------------------------------------------------
@app.post("/api/laser/connect")
async def laser_connect(req: LaserConnectReq):
    await asyncio.to_thread(_laser.connect, req.port)
    return {"ok": True}


@app.post("/api/laser/disconnect")
async def laser_disconnect():
    await asyncio.to_thread(_laser.disconnect)
    return {"ok": True}


@app.get("/api/laser/status")
async def laser_status():
    return await asyncio.to_thread(_laser.get_status)


@app.post("/api/laser/power")
async def laser_power(req: LaserPowerReq):
    await asyncio.to_thread(_laser.set_power, req.power_mw)
    return {"ok": True}


@app.post("/api/laser/enable")
async def laser_enable(req: LaserEnableReq):
    await asyncio.to_thread(_laser.set_enabled, req.enabled)
    return {"ok": True}


@app.post("/api/laser/mode")
async def laser_mode(req: LaserModeReq):
    await asyncio.to_thread(_laser.set_modulation_mode, req.mode)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Galvo
# ---------------------------------------------------------------------------
@app.post("/api/galvo/connect")
async def galvo_connect(req: GalvoConnectReq):
    await asyncio.to_thread(_galvo.connect, req.x_channel, req.y_channel)
    return {"ok": True}


@app.post("/api/galvo/disconnect")
async def galvo_disconnect():
    await asyncio.to_thread(_galvo.disconnect)
    return {"ok": True}


@app.get("/api/galvo/position")
async def galvo_position():
    x, y = _galvo.get_position()
    return {"x_v": x, "y_v": y}


@app.post("/api/galvo/move")
async def galvo_move(req: GalvoMoveReq):
    await asyncio.to_thread(_galvo.move_to, req.x_v, req.y_v)
    return {"ok": True}


@app.post("/api/galvo/center")
async def galvo_center():
    await asyncio.to_thread(_galvo.center)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------
@app.post("/api/trigger/connect")
async def trigger_connect(req: TriggerConnectReq):
    await asyncio.to_thread(_trigger.connect, req.channel)
    return {"ok": True}


@app.post("/api/trigger/disconnect")
async def trigger_disconnect():
    await asyncio.to_thread(_trigger.disconnect)
    return {"ok": True}


@app.post("/api/trigger/start")
async def trigger_start(req: TriggerStartReq):
    await asyncio.to_thread(_trigger.start, req.freq_hz, req.duty_cycle)
    return {"ok": True}


@app.post("/api/trigger/stop")
async def trigger_stop():
    await asyncio.to_thread(_trigger.stop)
    return {"ok": True}


@app.get("/api/trigger/status")
async def trigger_status():
    return _trigger.get_status()


# ---------------------------------------------------------------------------
# Scope — REST
# ---------------------------------------------------------------------------
@app.post("/api/scope/connect")
async def scope_connect():
    await asyncio.to_thread(_scope.connect)
    return {"ok": True}


@app.post("/api/scope/disconnect")
async def scope_disconnect():
    await asyncio.to_thread(_scope.disconnect)
    return {"ok": True}


@app.post("/api/scope/configure")
async def scope_configure(req: ScopeConfigReq):
    await asyncio.to_thread(
        _scope.configure_channel, req.channel, req.coupling, req.range_label
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scope — WebSocket streaming
#
# Binary frame format (little-endian):
#   [4 B]  uint32  n_samples
#   [4*n B] float32[] time_us
#   [4*n B] float32[] voltage_mv
# ---------------------------------------------------------------------------
def _pack_frame(t: np.ndarray, v: np.ndarray) -> bytes:
    n = len(t)
    return (
        struct.pack("<I", n)
        + t.astype(np.float32).tobytes()
        + v.astype(np.float32).tobytes()
    )


@app.websocket("/ws/scope")
async def scope_ws(ws: WebSocket):
    await ws.accept()
    stop_evt = asyncio.Event()
    acq_task: asyncio.Task | None = None

    async def _capture_loop(cfg: dict) -> None:
        while not stop_evt.is_set():
            try:
                t, v = await asyncio.to_thread(
                    _scope.capture_block,
                    cfg["sample_rate_hz"],
                    cfg["duration_ms"],
                    cfg.get("trigger_mv", 0.0),
                )
                await ws.send_bytes(_pack_frame(t, v))
            except WebSocketDisconnect:
                stop_evt.set()
                return
            except Exception as exc:
                try:
                    await ws.send_json({"error": str(exc)})
                except Exception:
                    pass
                stop_evt.set()
                return

    try:
        while True:
            msg = await ws.receive_json()
            cmd = msg.get("type")

            if cmd == "single":
                # Stop any ongoing stream first
                stop_evt.set()
                if acq_task:
                    await asyncio.shield(acq_task)
                    acq_task = None
                stop_evt.clear()
                _scope.configure_channel(
                    msg.get("channel", "A"),
                    msg.get("coupling", "DC"),
                    msg.get("range_label", "500 mV"),
                )
                t, v = await asyncio.to_thread(
                    _scope.capture_block,
                    msg["sample_rate_hz"],
                    msg["duration_ms"],
                    msg.get("trigger_mv", 0.0),
                )
                await ws.send_bytes(_pack_frame(t, v))

            elif cmd == "start_continuous":
                stop_evt.set()
                if acq_task:
                    await asyncio.shield(acq_task)
                stop_evt.clear()
                _scope.configure_channel(
                    msg.get("channel", "A"),
                    msg.get("coupling", "DC"),
                    msg.get("range_label", "500 mV"),
                )
                acq_task = asyncio.create_task(_capture_loop(msg))

            elif cmd == "stop":
                stop_evt.set()
                if acq_task:
                    await asyncio.shield(acq_task)
                    acq_task = None

    except WebSocketDisconnect:
        stop_evt.set()
        if acq_task:
            acq_task.cancel()
