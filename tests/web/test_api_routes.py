"""TestClient coverage: board control, streaming, graph export, SSE, middleware, static."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from collections.abc import Callable, MutableMapping
from typing import Any

import pytest
from fakes import FakeBoardManager
from fastapi.testclient import TestClient
from starlette.types import ASGIApp

from plugins.adc_6ch_12bit.config import DEFAULT_CONFIG
from plugins.adc_6ch_12bit.driver import Sample

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-data"


# -- boards / connect / status -------------------------------------------------


def test_list_boards(client: TestClient, fake_manager: FakeBoardManager) -> None:
    response = client.get("/api/boards")
    assert response.status_code == 200
    assert response.json() == [
        {
            "device": "COM7",
            "serial": "SN123",
            "firmware": "0.2",
            "channels": 6,
            "resolution": 12,
        }
    ]
    assert fake_manager.called("discover") == [()]


def test_connect_success(client: TestClient, fake_manager: FakeBoardManager) -> None:
    response = client.post("/api/connect", json={"device": "COM7"})
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert fake_manager.called("connect") == [("COM7",)]


def test_connect_manager_error_maps_to_400(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    fake_manager.connect_error = "no ADC board at COM9"
    response = client.post("/api/connect", json={"device": "COM9"})
    assert response.status_code == 400
    assert response.json()["detail"] == "no ADC board at COM9"


def test_status(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["channel_mask"] == 0x3F
    assert body["streaming"] is False


# -- config ---------------------------------------------------------------------


def test_get_config_returns_manager_config(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json() == fake_manager.config


def test_put_config_typed_settings_roundtrip(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    """#6: null keeps its meaning and well-typed settings are accepted."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(
        {"retention_days": None, "graph_width": None, "channel_mask": 3, "graph_points": 50}
    )
    response = client.put("/api/config", json=config)
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["channel_mask"] == 3


def test_put_config_valid_roundtrips(client: TestClient, fake_manager: FakeBoardManager) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["profiles"]["default"]["channels"]["A0"]["name"] = "Rail 3V3"
    response = client.put("/api/config", json=config)
    assert response.status_code == 200
    assert response.json()["profiles"]["default"]["channels"]["A0"]["name"] == "Rail 3V3"
    assert fake_manager.called("set_config") == [(config,)]


def _drop_profiles(config: dict[str, Any]) -> None:
    del config["profiles"]


def _unknown_active_profile(config: dict[str, Any]) -> None:
    config["active_profile"] = "nope"


def _missing_channel(config: dict[str, Any]) -> None:
    del config["profiles"]["default"]["channels"]["A3"]


def _non_numeric_gain(config: dict[str, Any]) -> None:
    config["profiles"]["default"]["channels"]["A1"]["gain"] = "high"


def _malformed_expression(config: dict[str, Any]) -> None:
    config["profiles"]["default"]["computed"] = [
        {"name": "Evil", "unit": "W", "expr": "__import__('os')", "show": True}
    ]


def _negative_sample_rate(config: dict[str, Any]) -> None:
    config["settings"]["sample_rate_hz"] = -5


def _string_retention(config: dict[str, Any]) -> None:
    config["settings"]["retention_days"] = "7"  # #6: TypeError in every listing


def _zero_retention(config: dict[str, Any]) -> None:
    config["settings"]["retention_days"] = 0


def _boolean_retention(config: dict[str, Any]) -> None:
    config["settings"]["retention_days"] = True


def _string_channel_mask(config: dict[str, Any]) -> None:
    config["settings"]["channel_mask"] = "3"


def _oversize_channel_mask(config: dict[str, Any]) -> None:
    config["settings"]["channel_mask"] = 64


def _string_graph_points(config: dict[str, Any]) -> None:
    config["settings"]["graph_points"] = "300"


def _string_graph_scroll(config: dict[str, Any]) -> None:
    config["settings"]["graph_scroll"] = "yes"


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_profiles,
        _unknown_active_profile,
        _missing_channel,
        _non_numeric_gain,
        _malformed_expression,
        _negative_sample_rate,
        _string_retention,
        _zero_retention,
        _boolean_retention,
        _string_channel_mask,
        _oversize_channel_mask,
        _string_graph_points,
        _string_graph_scroll,
    ],
)
def test_put_config_invalid_is_422(
    client: TestClient,
    fake_manager: FakeBoardManager,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    mutate(config)
    response = client.put("/api/config", json=config)
    assert response.status_code == 422
    assert fake_manager.config == DEFAULT_CONFIG  # nothing was applied


# -- averaging / channels ---------------------------------------------------------


def test_set_averaging_valid(client: TestClient, fake_manager: FakeBoardManager) -> None:
    response = client.post("/api/averaging", json={"n": 16})
    assert response.status_code == 200
    assert fake_manager.called("set_averaging") == [(16,)]


def test_set_averaging_invalid_is_422(client: TestClient, fake_manager: FakeBoardManager) -> None:
    response = client.post("/api/averaging", json={"n": 3})
    assert response.status_code == 422
    assert fake_manager.called("set_averaging") == []


def test_set_channels_valid(client: TestClient, fake_manager: FakeBoardManager) -> None:
    response = client.post("/api/channels", json={"mask": 0x0F})
    assert response.status_code == 200
    assert fake_manager.called("set_channels") == [(0x0F,)]


@pytest.mark.parametrize("mask", [-1, 0x40])
def test_set_channels_invalid_is_422(
    client: TestClient, fake_manager: FakeBoardManager, mask: int
) -> None:
    response = client.post("/api/channels", json={"mask": mask})
    assert response.status_code == 422
    assert fake_manager.called("set_channels") == []


# -- stream control -----------------------------------------------------------------


@pytest.mark.parametrize("record", [True, False])
def test_stream_start(client: TestClient, fake_manager: FakeBoardManager, record: bool) -> None:
    response = client.post("/api/stream/start", json={"record": record, "note": "bench"})
    assert response.status_code == 200
    body = response.json()
    assert body["streaming"] is True
    assert body["recording"] is record
    assert fake_manager.called("start_stream") == [(record, "bench")]


def test_stream_start_without_body_defaults_to_no_recording(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    response = client.post("/api/stream/start")
    assert response.status_code == 200
    assert response.json()["recording"] is False
    assert fake_manager.called("start_stream") == [(False, "")]


def test_stream_stop(client: TestClient, fake_manager: FakeBoardManager) -> None:
    client.post("/api/stream/start", json={"record": True})
    response = client.post("/api/stream/stop")
    assert response.status_code == 200
    body = response.json()
    assert body["streaming"] is False
    assert body["recording"] is False
    assert fake_manager.called("stop_stream") == [()]


def test_stream_pause_and_resume(client: TestClient, fake_manager: FakeBoardManager) -> None:
    client.post("/api/stream/start")
    paused = client.post("/api/stream/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    resumed = client.post("/api/stream/resume")
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert fake_manager.called("pause_stream") == [()]
    assert fake_manager.called("resume_stream") == [()]


# -- graph export / reveal -------------------------------------------------------------


def test_graph_export_valid_base64(client: TestClient, fake_manager: FakeBoardManager) -> None:
    image = base64.b64encode(PNG_BYTES).decode("ascii")
    response = client.post("/api/graph/export", json={"image": image})
    assert response.status_code == 200
    assert response.json()["name"] == "graph.png"
    assert fake_manager.called("save_graph_png") == [(PNG_BYTES,)]


def test_graph_export_strips_data_uri_prefix(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    image = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    response = client.post("/api/graph/export", json={"image": image})
    assert response.status_code == 200
    assert fake_manager.called("save_graph_png") == [(PNG_BYTES,)]


def test_graph_export_invalid_base64_is_422(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    response = client.post("/api/graph/export", json={"image": "not-base64!!!"})
    assert response.status_code == 422
    assert fake_manager.called("save_graph_png") == []


def test_graph_export_oversized_decoded_payload_is_413(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    from benchweave.web import app as web_app

    image = base64.b64encode(b"\x00" * (web_app.MAX_PNG_BYTES + 1)).decode("ascii")
    response = client.post("/api/graph/export", json={"image": image})
    assert response.status_code == 413
    assert fake_manager.called("save_graph_png") == []


def test_graph_reveal(client: TestClient, fake_manager: FakeBoardManager) -> None:
    response = client.post("/api/graph/reveal")
    assert response.status_code == 200
    assert response.json() == {"path": "captures"}
    assert fake_manager.called("reveal_graph_png") == [()]


def test_graph_reveal_error_maps_to_500(client: TestClient, fake_manager: FakeBoardManager) -> None:
    fake_manager.reveal_error = "no file manager available"
    response = client.post("/api/graph/reveal")
    assert response.status_code == 500
    assert response.json()["detail"] == "no file manager available"


# -- SSE live stream ---------------------------------------------------------------------


async def _drive_sse(app: ASGIApp, frames_wanted: int) -> tuple[int, dict[str, str], list[str]]:
    """Run one GET /api/stream against the raw ASGI app, as a client that
    disconnects once ``frames_wanted`` SSE data frames have arrived.

    The endpoint streams forever, so the in-process TestClient (which buffers
    the whole response) cannot consume it; driving the ASGI interface directly
    lets the ``http.disconnect`` message end the stream the way a real server
    would, exercising the ``is_disconnected`` branch and the unsubscribe
    cleanup.
    """
    status: int | None = None
    headers: dict[str, str] = {}
    chunks: list[str] = []
    body_sent = False
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update({key.decode(): value.decode() for key, value in message["headers"]})
        elif message["type"] == "http.response.body":
            chunk = bytes(message.get("body", b""))
            if chunk:
                chunks.append(chunk.decode("utf-8"))
            if sum(c.startswith("data: ") for c in chunks) >= frames_wanted:
                disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=15.0)
    assert status is not None
    return status, headers, chunks


@pytest.mark.timeout(30)
def test_stream_sse_frames_and_cleanup(
    client: TestClient, fake_manager: FakeBoardManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchweave.web import app as web_app

    # Defeat the ~30 Hz downsampling: the pre-loaded samples all arrive "at once".
    monkeypatch.setattr(web_app, "SSE_MIN_INTERVAL", 0.0)
    for i in range(3):
        fake_manager.queue.put_nowait(
            Sample(counter=i, channels=(100 + i, 0, 0, 0, 0, 0), averaged_n=4)
        )

    status, headers, chunks = asyncio.run(_drive_sse(web_app.app, frames_wanted=3))

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(chunk.removeprefix("data: ")) for chunk in chunks if chunk.startswith("data: ")
    ]
    assert [frame["counter"] for frame in frames] == [0, 1, 2]
    assert [frame["averaged_n"] for frame in frames] == [4, 4, 4]
    assert frames[0]["channels"] == [
        {"key": "A0", "name": "Voltage", "unit": "V", "value": round(100 * 0.001221, 6)}
    ]
    # The client disconnect ended the endless stream and cleaned up the queue.
    assert fake_manager.unsubscribed == [fake_manager.queue]


def test_stream_sse_subscribe_error_is_503(
    client: TestClient, fake_manager: FakeBoardManager
) -> None:
    fake_manager.subscribe_error = "too many live-stream subscribers"
    response = client.get("/api/stream")
    assert response.status_code == 503
    assert response.json()["detail"] == "too many live-stream subscribers"


# -- body-size middleware / static mount ---------------------------------------------------


def test_oversized_request_body_is_rejected_by_middleware(client: TestClient) -> None:
    from benchweave.web import app as web_app

    # A forged Content-Length is enough: the middleware rejects on the header
    # alone, before the body (or any route) is read.
    response = client.post(
        "/api/connect",
        content=b"{}",
        headers={
            "Content-Length": str(web_app.MAX_BODY_BYTES + 1),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_static_index_served_at_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>BenchWeave ADC</title>" in response.text
