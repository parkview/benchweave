"""FastAPI web frontend for the BenchWeave ADC board."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from benchweave.web.board import BoardManager
from benchweave.web.library import CaptureLibrary
from benchweave.web.report import build_report
from plugins.adc_6ch_12bit.protocol import AVERAGING_CHOICES, CHANNEL_MASK_ALL

STATIC_DIR = Path(__file__).parent / "static"
SSE_MIN_INTERVAL = 0.033  # downsample the live view to ~30 Hz

manager = BoardManager()
library = CaptureLibrary()


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
    note: str = ""


class GraphExportBody(BaseModel):
    image: str  # base64-encoded PNG (no data: URI prefix)


class ProjectCreateBody(BaseModel):
    name: str
    retention_days: int | None = None


class ProjectRetentionBody(BaseModel):
    retention_days: int | None = None


class AssignProjectBody(BaseModel):
    project: str | None = None


class TrashBody(BaseModel):
    stems: list[str]


class AnnotationsBody(BaseModel):
    markers: list[dict[str, Any]]


class PowerRailBody(BaseModel):
    v: str | None = None
    i: str | None = None


class PowerReportBody(BaseModel):
    mode: str = "battery"
    rails: list[PowerRailBody] = []
    capacity_ah: float | None = None
    threshold: float | None = None
    lo: float | None = None
    hi: float | None = None


class ZoomBody(BaseModel):
    lo: float | None = None
    hi: float | None = None


class ReportBody(BaseModel):
    lo: float | None = None
    hi: float | None = None
    power: PowerReportBody | None = None
    zoom: ZoomBody | None = None


class PowerBody(BaseModel):
    mode: str = "battery"
    rails: list[dict[str, Any]] = []


class DefaultModeBody(BaseModel):
    mode: str = "battery"


class AssertionsBody(BaseModel):
    assertions: list[dict[str, Any]]


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


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return manager.get_config()


@app.put("/api/config")
def put_config(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return manager.set_config(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/averaging")
def set_averaging(body: AveragingBody) -> dict[str, object]:
    if body.n not in AVERAGING_CHOICES:
        raise HTTPException(status_code=422, detail=f"averaging must be one of {AVERAGING_CHOICES}")
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
        return manager.start_stream(
            record=body.record if body else False, note=body.note if body else ""
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/stop")
def stream_stop() -> dict[str, object]:
    return manager.stop_stream()


@app.post("/api/stream/pause")
def stream_pause() -> dict[str, object]:
    try:
        return manager.pause_stream()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/resume")
def stream_resume() -> dict[str, object]:
    try:
        return manager.resume_stream()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/graph/export")
def graph_export(body: GraphExportBody) -> dict[str, object]:
    image = body.image
    if "," in image:
        image = image.split(",", 1)[1]
    try:
        data = base64.b64decode(image, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="invalid PNG data") from exc
    return manager.save_graph_png(data)


@app.post("/api/graph/reveal")
def graph_reveal() -> dict[str, object]:
    try:
        return manager.reveal_graph_png()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
                    continue  # cap the live view at ~30 Hz
                last = now
                payload = {
                    "counter": sample.counter,
                    "averaged_n": sample.averaged_n,
                    "channels": manager.convert_sample(sample),
                }
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream")


# -- capture library (Analyse tab) -----------------------------------------


@app.get("/api/captures")
def list_captures() -> list[dict[str, object]]:
    return library.scan()


@app.get("/api/captures/storage")
def capture_storage() -> dict[str, object]:
    return library.storage_stats()


@app.get("/api/captures/retention")
def retention_suggestions() -> dict[str, object]:
    return {"expired": library.retention_scan()}


@app.get("/api/captures/{stem}/data")
def capture_data(stem: str) -> dict[str, object]:
    path = library.file_for(stem, "csv")
    if path is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    try:
        return library.parse_csv(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/captures/{stem}/file")
def capture_file(stem: str, ext: str = "csv") -> FileResponse:
    if ext not in ("csv", "png", "html"):
        raise HTTPException(status_code=422, detail="ext must be csv, png, or html")
    path = library.file_for(stem, ext)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no {ext} for '{stem}'")
    return FileResponse(path)


@app.get("/api/captures/{stem}/annotations")
def get_annotations(stem: str) -> dict[str, object]:
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return {"markers": library.get_annotations(stem)}


@app.put("/api/captures/{stem}/annotations")
def set_annotations(stem: str, body: AnnotationsBody) -> dict[str, object]:
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return {"markers": library.set_annotations(stem, body.markers)}


@app.get("/api/captures/{stem}/power")
def get_power(stem: str) -> dict[str, object]:
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return library.get_power(stem)


@app.put("/api/captures/{stem}/power")
def set_power(stem: str, body: PowerBody) -> dict[str, object]:
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return library.set_power(stem, body.model_dump())


@app.put("/api/power/default")
def set_power_default(body: DefaultModeBody) -> dict[str, object]:
    return {"mode": library.set_default_mode(body.mode)}


@app.get("/api/assertions")
def get_assertions() -> dict[str, object]:
    return {"assertions": library.get_assertions()}


@app.put("/api/assertions")
def set_assertions(body: AssertionsBody) -> dict[str, object]:
    return {"assertions": library.set_assertions(body.assertions)}


@app.get("/api/captures/{stem}/assertions")
def capture_assertions(stem: str) -> dict[str, object]:
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return library.check_assertions(stem)


@app.post("/api/captures/{stem}/report")
def generate_report(stem: str, body: ReportBody) -> dict[str, object]:
    csv = library.file_for(stem, "csv")
    if csv is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    data = library.parse_csv(csv)
    markers = library.get_annotations(stem)
    lo, hi = body.lo, body.hi
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    zoom = None
    if body.zoom is not None and body.zoom.lo is not None and body.zoom.hi is not None:
        zoom = (
            min(body.zoom.lo, body.zoom.hi),
            max(body.zoom.lo, body.zoom.hi),
        )
    assertions = cast(list[dict[str, Any]], library.check_assertions(stem)["results"])
    html = build_report(
        data,
        markers,
        lo,
        hi,
        body.power.model_dump() if body.power else None,
        assertions,
        zoom,
    )
    out = csv.with_suffix(".html")
    out.write_text(html)
    return {"stem": stem, "name": out.name, "path": str(out)}


@app.post("/api/captures/{stem}/project")
def capture_assign(stem: str, body: AssignProjectBody) -> dict[str, object]:
    try:
        return library.assign_project(stem, body.project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/captures/trash")
def capture_trash(body: TrashBody) -> dict[str, object]:
    if not body.stems:
        raise HTTPException(status_code=422, detail="no stems to trash")
    return library.trash(body.stems)


@app.post("/api/captures/{stem}/apply-config")
def capture_apply_config(stem: str) -> dict[str, Any]:
    path = library.file_for(stem, "csv")
    if path is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    config = library.config_from_csv(path)
    if config is None:
        raise HTTPException(status_code=400, detail=f"no config metadata in '{stem}'")
    try:
        return manager.set_config(config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects")
def list_projects() -> list[dict[str, object]]:
    return library.list_projects()


@app.post("/api/projects")
def create_project(body: ProjectCreateBody) -> dict[str, object]:
    try:
        return library.create_project(body.name, body.retention_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/projects/{name}")
def patch_project(name: str, body: ProjectRetentionBody) -> dict[str, object]:
    try:
        return library.set_project_retention(name, body.retention_days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
