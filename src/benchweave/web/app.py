"""FastAPI web frontend for the BenchWeave ADC board."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit.protocol import AVERAGING_CHOICES, CHANNEL_MASK_ALL

STATIC_DIR = Path(__file__).parent / "static"
SSE_MIN_INTERVAL = 0.033  # downsample the live view to ~30 Hz

manager = BoardManager()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    manager.set_loop(asyncio.get_running_loop())
    yield
    manager.disconnect()


app = FastAPI(title="BenchWeave ADC", lifespan=lifespan)


class ConnectBody(BaseModel):
    device: str


class AveragingBody(BaseModel):
    n: int


class ChannelsBody(BaseModel):
    mask: int


class StreamStartBody(BaseModel):
    record: bool = False


@app.get("/api/boards")
def list_boards() -> list[dict[str, object]]:
    return manager.discover()


@app.post("/api/connect")
def connect(body: ConnectBody) -> dict[str, object]:
    try:
        return manager.connect(body.device)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/status")
def status() -> dict[str, object]:
    return manager.status()


@app.post("/api/averaging")
def set_averaging(body: AveragingBody) -> dict[str, object]:
    if body.n not in AVERAGING_CHOICES:
        raise HTTPException(
            status_code=422, detail=f"averaging must be one of {AVERAGING_CHOICES}"
        )
    try:
        return manager.set_averaging(body.n)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/channels")
def set_channels(body: ChannelsBody) -> dict[str, object]:
    if not 0 <= body.mask <= CHANNEL_MASK_ALL:
        raise HTTPException(status_code=422, detail=f"mask must be 0..{CHANNEL_MASK_ALL}")
    try:
        return manager.set_channels(body.mask)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/start")
def stream_start(body: StreamStartBody | None = None) -> dict[str, object]:
    try:
        return manager.start_stream(record=body.record if body else False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/stop")
def stream_stop() -> dict[str, object]:
    return manager.stop_stream()


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    queue = manager.subscribe()

    async def events() -> AsyncIterator[str]:
        last = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    sample = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    continue
                now = time.monotonic()
                if now - last < SSE_MIN_INTERVAL:
                    continue  # downsample: drop samples faster than ~30 Hz
                last = now
                payload = {
                    "counter": sample.counter,
                    "averaged_n": sample.averaged_n,
                    "channels": list(sample.channels),
                }
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
