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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from benchweave.web.board import BoardManager
from benchweave.web.library import CaptureLibrary
from benchweave.web.report import build_report
from plugins.adc_6ch_12bit.protocol import AVERAGING_CHOICES, CHANNEL_MASK_ALL

STATIC_DIR = Path(__file__).parent / "static"
SSE_MIN_INTERVAL = 0.033  # downsample the live view to ~30 Hz
MAX_BODY_BYTES = 20 * 1024 * 1024  # request-body ceiling (graph PNGs are the largest)
MAX_PNG_BYTES = 10 * 1024 * 1024  # decoded graph-export image ceiling
MAX_DATA_POINTS = 5000  # default per-channel points served by /data

manager = BoardManager()
library = CaptureLibrary()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Bind the board manager to the app's event loop; disconnect on shutdown."""
    manager.set_loop(asyncio.get_running_loop())
    yield
    manager.disconnect()


app = FastAPI(
    title="BenchWeave ADC",
    description=(
        "Capture and analysis gateway for the 6-channel, 12-bit ADC board: "
        "board control and live streaming, a capture library with projects "
        "and retention, power analysis, and HTML report export. Built for a "
        "single operator on localhost - there is no authentication, CORS "
        "policy, or CSRF protection. Request bodies over 20 MiB are "
        "rejected with 413; chunked uploads with no declared length are "
        "rejected with 411."
    ),
    lifespan=lifespan,
)


class _BodyTooLarge(HTTPException):
    """Raised from the metered receive channel once a body outgrows the cap."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


class BodyLimitMiddleware:
    """Reject request bodies over ``MAX_BODY_BYTES``, however they are framed.

    A numeric ``Content-Length`` over the cap is rejected up front with 413 and
    ``Transfer-Encoding: chunked`` is refused with 411 (uvicorn/h11 never
    delivers more bytes than a declared length, so those two checks already
    bound every HTTP/1.1 body). As a server-agnostic backstop the receive
    channel is also metered, so a body that outgrows the cap mid-stream is cut
    off with a 413 no matter how the server framed it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if "chunked" in headers.get("transfer-encoding", "").lower():
            response = JSONResponse({"detail": "length required"}, status_code=411)
            await response(scope, receive, send)
            return
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            response = JSONResponse({"detail": "request body too large"}, status_code=413)
            await response(scope, receive, send)
            return

        received = 0
        response_started = False

        async def metered_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, metered_receive, tracking_send)
        except _BodyTooLarge:
            # FastAPI routes re-raise HTTPException from their body read, so
            # the router already answered with 413; this backstop covers body
            # reads outside the router.
            if response_started:
                raise
            response = JSONResponse({"detail": "request body too large"}, status_code=413)
            await response(scope, receive, send)


app.add_middleware(BodyLimitMiddleware)


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
    """Probe candidate serial ports with IDENTIFY and list the ADC boards found.

    Each entry carries the device path, board serial, firmware version,
    channel count, and resolution. Probing touches the serial ports, so it is
    serialised against any active capture."""
    return manager.discover()


@app.post("/api/connect")
def connect(body: ConnectBody) -> dict[str, object]:
    """Connect to a board by serial device path and return the new status.

    Opens the port, identifies the firmware, and re-applies the persisted
    channel selection. Any existing session is closed first. Returns 400 when
    the port cannot be opened or the board does not answer."""
    try:
        return manager.connect(body.device)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/status")
def status() -> dict[str, object]:
    """Return the board status: connection, firmware, averaging, channel mask,
    streaming/paused/recording flags, the last stream error, and the estimated
    maximum sample rate."""
    return manager.status()


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """Return the full runtime configuration: measurement profiles (per-channel
    name/unit/gain/offset/show/colour and computed channels) plus settings."""
    return manager.get_config()


@app.put("/api/config")
def put_config(body: dict[str, Any]) -> dict[str, Any]:
    """Replace the runtime configuration and persist it to the plugin's
    config.json. The config is validated first (422 on a config that would
    later break recording or conversion); other failures return 400."""
    try:
        return manager.set_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/averaging")
def set_averaging(body: AveragingBody) -> dict[str, object]:
    """Set the board's hardware averaging depth and return the new status.

    422 unless ``n`` is a supported choice ({0, 4, 8, ..., 256}); 400 when no
    board is connected or a stream is running (stop streaming first)."""
    if body.n not in AVERAGING_CHOICES:
        raise HTTPException(status_code=422, detail=f"averaging must be one of {AVERAGING_CHOICES}")
    try:
        return manager.set_averaging(body.n)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/channels")
def set_channels(body: ChannelsBody) -> dict[str, object]:
    """Set the enabled-channel bitmask (bits 0-4 = A0-A4, bit 5 = A7) and
    persist it in the config settings. 422 when the mask is outside 1..63; 400
    when no board is connected or a stream is running."""
    if not 1 <= body.mask <= CHANNEL_MASK_ALL:
        raise HTTPException(status_code=422, detail=f"mask must be 1..{CHANNEL_MASK_ALL}")
    try:
        return manager.set_channels(body.mask)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/start")
def stream_start(body: StreamStartBody | None = None) -> dict[str, object]:
    """Start live streaming from the board (no-op if already streaming).

    With ``record: true`` a CSV capture is opened in the captures directory,
    with the optional ``note`` embedded in its metadata header. Returns 400
    when no board is connected."""
    try:
        return manager.start_stream(
            record=body.record if body else False, note=body.note if body else ""
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/stop")
def stream_stop() -> dict[str, object]:
    """Stop streaming and close any open CSV recording. Idempotent - stopping
    an idle board just returns the current status."""
    return manager.stop_stream()


@app.post("/api/stream/pause")
def stream_pause() -> dict[str, object]:
    """Pause a running stream: data collection halts but any recording CSV
    stays open. A no-op unless streaming and not already paused; 400 when no
    board is connected."""
    try:
        return manager.pause_stream()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stream/resume")
def stream_resume() -> dict[str, object]:
    """Resume a paused stream into the same CSV file, with elapsed time
    continuous across the pause gap. A no-op unless paused; 400 when no board
    is connected."""
    try:
        return manager.resume_stream()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/graph/export")
def graph_export(body: GraphExportBody) -> dict[str, object]:
    """Save a base64-encoded PNG of the live graph to the captures directory,
    named after the current recording (or a fresh capture filename).

    422 when the payload is not valid base64; 413 when the decoded image
    exceeds 10 MiB."""
    image = body.image
    if "," in image:
        image = image.split(",", 1)[1]
    try:
        data = base64.b64decode(image, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="invalid PNG data") from exc
    if len(data) > MAX_PNG_BYTES:
        raise HTTPException(status_code=413, detail="image too large")
    return manager.save_graph_png(data)


@app.post("/api/graph/reveal")
def graph_reveal() -> dict[str, object]:
    """Open the operating system's file manager at the most recently exported
    PNG (or the captures directory when none was exported yet). 500 when no
    file manager is available on the host."""
    try:
        return manager.reveal_graph_png()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events feed of live samples, converted per the active
    profile and downsampled to at most ~30 Hz per subscriber.

    Each event carries the sample counter, averaging depth, and per-channel
    values. 503 once the subscriber cap (32) is reached."""
    try:
        queue = manager.subscribe()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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
    """List every capture file (CSV/PNG/HTML) newest first, with its project,
    retention, and expiry flag. Also reconciles the SQLite overlay with the
    files currently on disk."""
    return library.scan()


@app.get("/api/captures/storage")
def capture_storage() -> dict[str, object]:
    """Return total capture storage (bytes and file count) with a per-project
    breakdown; unassigned files are grouped under "(unassigned)"."""
    return library.storage_stats()


@app.get("/api/captures/retention")
def retention_suggestions() -> dict[str, object]:
    """Return the stems of captures that have reached their retention date
    (per-project retention, or the global default for unassigned captures).
    Advisory only - nothing is deleted here."""
    return {"expired": library.retention_scan()}


@app.get("/api/captures/{stem}/data")
def capture_data(
    stem: str,
    # Bounded by the route (#8): 0, a negative value or an oversize value used
    # to return the whole series, so the cap only held for cooperative clients.
    max_points: int = Query(MAX_DATA_POINTS, ge=1, le=MAX_DATA_POINTS),
) -> dict[str, object]:
    """Parse a capture's CSV and return its metadata and per-channel series.

    ``max_points`` decimates each channel to at most that many points
    (default 5000, allowed 1..5000; anything outside that range is 422).
    404 when the stem has no CSV; 400 when the file cannot be parsed."""
    path = library.file_for(stem, "csv")
    if path is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    try:
        data = library.parse_csv(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data["series"] = [
        {**s, "points": _decimate(cast(list[Any], s["points"]), max_points)}
        for s in cast(list[dict[str, Any]], data["series"])
    ]
    return data


def _decimate(points: list[Any], limit: int) -> list[Any]:
    """Every n-th point so at most ``limit`` survive; the final point replaces
    the last stride sample when the budget is full."""
    if len(points) <= limit:
        return points
    step = (len(points) + limit - 1) // limit
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        if len(sampled) >= limit:
            sampled[-1] = points[-1]
        else:
            sampled.append(points[-1])
    return sampled


@app.get("/api/captures/{stem}/file")
def capture_file(stem: str, ext: str = "csv") -> FileResponse:
    """Serve a capture file verbatim. 422 unless ``ext`` is csv, png, or html;
    404 when the file does not exist. HTML is served with a sandboxing
    Content-Security-Policy so stored reports cannot script against this
    API."""
    if ext not in ("csv", "png", "html"):
        raise HTTPException(status_code=422, detail="ext must be csv, png, or html")
    path = library.file_for(stem, ext)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no {ext} for '{stem}'")
    response = FileResponse(path)
    if ext == "html":
        # Serve stored HTML in a unique origin: report files (or anything
        # dropped into the captures directory) must not script against the
        # gateway API. allow-scripts keeps the report itself working.
        response.headers["Content-Security-Policy"] = "sandbox allow-scripts"
    return response


@app.get("/api/captures/{stem}/annotations")
def get_annotations(stem: str) -> dict[str, object]:
    """Return the capture's A-Z letter markers (label, time, note); an empty
    list when none are saved. 404 when the stem has no CSV."""
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return {"markers": library.get_annotations(stem)}


@app.put("/api/captures/{stem}/annotations")
def set_annotations(stem: str, body: AnnotationsBody) -> dict[str, object]:
    """Replace the capture's letter markers. Markers are validated (unique
    single letters A-Z, finite non-negative times) and stored in the library
    database; the cleaned list is returned. 404 when the stem has no CSV."""
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return {"markers": library.set_annotations(stem, body.markers)}


@app.get("/api/captures/{stem}/power")
def get_power(stem: str) -> dict[str, object]:
    """Return the capture's power-analysis setup (mode and V/I rail pairing).

    Falls back to the most recent setup whose rails match this capture's
    channels, then to the global default mode; ``source`` says which
    ("capture", "reused", or "default"). 404 when the stem has no CSV."""
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return library.get_power(stem)


@app.put("/api/captures/{stem}/power")
def set_power(stem: str, body: PowerBody) -> dict[str, object]:
    """Save the capture's power-analysis setup (mode and up to two V/I rails)
    in the library database and return the cleaned state. Unknown modes fall
    back to "battery". 404 when the stem has no CSV."""
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return library.set_power(stem, body.model_dump())


@app.put("/api/power/default")
def set_power_default(body: DefaultModeBody) -> dict[str, object]:
    """Set the global default power-analysis mode used for captures with no
    saved setup. Unknown modes fall back to "battery"; the stored mode is
    returned."""
    return {"mode": library.set_default_mode(body.mode)}


@app.get("/api/assertions")
def get_assertions() -> dict[str, object]:
    """Return the global channel assertions: per-channel-name min/max bounds
    checked against every capture."""
    return {"assertions": library.get_assertions()}


@app.put("/api/assertions")
def set_assertions(body: AssertionsBody) -> dict[str, object]:
    """Replace the global channel assertions. Entries are validated (unique
    non-empty names, at least one finite bound, min/max swapped into order)
    and the cleaned list is stored and returned."""
    return {"assertions": library.set_assertions(body.assertions)}


@app.get("/api/captures/{stem}/assertions")
def capture_assertions(stem: str) -> dict[str, object]:
    """Evaluate the global assertions against one capture and return the
    per-assertion results (bounds, actual min/max, pass/fail) with a pass
    count. 404 when the stem has no CSV."""
    if library.file_for(stem, "csv") is None:
        raise HTTPException(status_code=404, detail=f"no CSV for '{stem}'")
    return library.check_assertions(stem)


@app.post("/api/captures/{stem}/report")
def generate_report(stem: str, body: ReportBody) -> dict[str, object]:
    """Build a self-contained HTML report for a capture and write it next to
    the CSV as ``<stem>.html`` (overwriting any previous report).

    The report embeds the chart, letter markers, assertion results, and -
    when given - the selected region's statistics, a power analysis, and a
    zoomed chart. 404 when the stem has no CSV."""
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
    assertions = cast(list[dict[str, Any]], library.check_assertions(stem, parsed=data)["results"])
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
    out.write_text(html, encoding="utf-8")
    return {"stem": stem, "name": out.name, "path": str(out)}


@app.post("/api/captures/{stem}/project")
def capture_assign(stem: str, body: AssignProjectBody) -> dict[str, object]:
    """Assign a capture (all files sharing the stem) to a project, or clear
    the assignment with a null project. 404 when no capture exists for the
    stem; 400 when the project does not exist."""
    if library.file_for(stem, "csv") is None and library.file_for(stem, "png") is None:
        raise HTTPException(status_code=404, detail=f"no capture '{stem}'")
    try:
        return library.assign_project(stem, body.project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/captures/trash")
def capture_trash(body: TrashBody) -> dict[str, object]:
    """Move every file of the given stems to the operating-system trash and
    report per-file successes and failures. Library metadata is deleted only
    for stems with no file of any kind left on disk - the single place rows
    are hard-deleted. 422 when the stem list is empty."""
    if not body.stems:
        raise HTTPException(status_code=422, detail="no stems to trash")
    return library.trash(body.stems)


@app.post("/api/captures/{stem}/apply-config")
def capture_apply_config(stem: str) -> dict[str, Any]:
    """Push the capture's recorded profile back to the connected board and
    persist it as the runtime configuration (newer captures embed the config;
    older ones are reconstructed from the ``#`` header). 404 when the stem has
    no CSV; 400 when the CSV carries no config metadata or the apply fails."""
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
    """List all projects (name and retention in days, null meaning "inherit
    the global default"), sorted by name."""
    return library.list_projects()


@app.post("/api/projects")
def create_project(body: ProjectCreateBody) -> dict[str, object]:
    """Create a project with an optional retention period in days (null
    inherits the global default). 400 when the name is empty or already
    taken."""
    try:
        return library.create_project(body.name, body.retention_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/projects/{name}")
def patch_project(name: str, body: ProjectRetentionBody) -> dict[str, object]:
    """Change a project's retention period (days; null inherits the global
    default). 404 when the project does not exist."""
    try:
        return library.set_project_retention(name, body.retention_days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
