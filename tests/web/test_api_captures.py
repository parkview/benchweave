"""TestClient coverage: capture library routes, reports, projects, trash, apply-config."""

from __future__ import annotations

import copy
import csv as csv_module
import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from benchweave.web.library import CaptureLibrary
from plugins.adc_6ch_12bit.config import DEFAULT_CONFIG
from tests.web.fakes import CSV, STEM, FakeBoardManager

UNKNOWN_STEM = "adc_none_20260101_000000"
HOSTILE = '" onmouseover="alert(1)'


def _write_capture(captures_dir: Path, stem: str = STEM, text: str = CSV) -> Path:
    path = captures_dir / f"{stem}.csv"
    # newline="" keeps the LF line endings byte-exact (test_capture_file_csv
    # compares the served bytes against the fixture text).
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _multirow_csv(rows: int) -> str:
    header = (
        "# profile: default\n"
        "# A0: Voltage (V)\n"
        "timestamp,elapsed_s,actual_sps,counter,averaged_n,Voltage\n"
    )
    lines = [
        f"2026-09-14T12:00:{i:02d}.000000,{float(i)},1.0,{i},0,{1.0 + i * 0.1:.1f}"
        for i in range(rows)
    ]
    return header + "\n".join(lines) + "\n"


# -- listing / storage / retention ------------------------------------------------


def test_list_captures(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    (captures_dir / f"{STEM}.png").write_bytes(b"\x89PNG-fake")
    response = client.get("/api/captures")
    assert response.status_code == 200
    records = response.json()
    assert {r["kind"] for r in records} == {"csv", "png"}
    assert all(r["stem"] == STEM for r in records)
    assert all(r["serial"] == "1234" for r in records)


def test_capture_storage(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    (captures_dir / f"{STEM}.png").write_bytes(b"\x89PNG-fake")
    response = client.get("/api/captures/storage")
    assert response.status_code == 200
    stats = response.json()
    assert stats["file_count"] == 2
    assert stats["total_bytes"] > 0
    assert stats["per_project"] == [
        {"project": "(unassigned)", "bytes": stats["total_bytes"], "count": 2}
    ]


def test_capture_retention(
    client: TestClient, captures_dir: Path, web_library: CaptureLibrary
) -> None:
    _write_capture(captures_dir)
    web_library.create_project("battery", retention_days=0)
    web_library.assign_project(STEM, "battery")
    response = client.get("/api/captures/retention")
    assert response.status_code == 200
    assert response.json() == {"expired": [STEM]}


# -- /data ---------------------------------------------------------------------------


def test_capture_data(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    response = client.get(f"/api/captures/{STEM}/data")
    assert response.status_code == 200
    data = response.json()
    assert data["sample_count"] == 2
    assert [s["name"] for s in data["series"]] == ["Voltage", "Current", "Power"]
    assert data["series"][0]["points"] == [[0.0, 1.0], [1.0, 1.1]]


def test_capture_data_decimates_to_max_points(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir, text=_multirow_csv(12))
    response = client.get(f"/api/captures/{STEM}/data", params={"max_points": 1})
    assert response.status_code == 200
    series = response.json()["series"]
    assert [s["name"] for s in series] == ["Voltage"]
    # The budget is a hard cap: at max_points=1 only the final point survives
    # (the endpoint replaces the last stride sample instead of appending).
    assert series[0]["points"] == [[11.0, 2.1]]


def test_capture_data_unknown_stem_is_404(client: TestClient) -> None:
    response = client.get(f"/api/captures/{UNKNOWN_STEM}/data")
    assert response.status_code == 404


def test_capture_data_traversal_stem_is_404(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    response = client.get("/api/captures/..%2Fetc/data")
    assert response.status_code == 404


# -- /file ------------------------------------------------------------------------------


def test_capture_file_csv(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    response = client.get(f"/api/captures/{STEM}/file")
    assert response.status_code == 200
    assert response.text == CSV
    assert "content-security-policy" not in response.headers


def test_capture_file_html_is_sandboxed(client: TestClient, captures_dir: Path) -> None:
    (captures_dir / f"{STEM}.html").write_text(
        "<html><script>alert(1)</script></html>", encoding="utf-8"
    )
    response = client.get(f"/api/captures/{STEM}/file", params={"ext": "html"})
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "sandbox allow-scripts"


def test_capture_file_bad_ext_is_422(client: TestClient) -> None:
    response = client.get(f"/api/captures/{STEM}/file", params={"ext": "exe"})
    assert response.status_code == 422


def test_capture_file_missing_is_404(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    response = client.get(f"/api/captures/{STEM}/file", params={"ext": "png"})
    assert response.status_code == 404


# -- annotations ---------------------------------------------------------------------------


def test_annotations_roundtrip_cleans_invalid_markers(
    client: TestClient, captures_dir: Path
) -> None:
    _write_capture(captures_dir)
    put = client.put(
        f"/api/captures/{STEM}/annotations",
        json={
            "markers": [
                {"label": "b", "t": 2.0, "note": "upper-cased"},
                {"label": "A", "t": 1.0, "note": "kept"},
                {"label": "A", "t": 9.0, "note": "duplicate dropped"},
                {"label": "1", "t": 3.0, "note": "non-letter dropped"},
                {"label": "C", "t": -1.0, "note": "negative t dropped"},
            ]
        },
    )
    assert put.status_code == 200
    expected = [
        {"label": "A", "t": 1.0, "note": "kept"},
        {"label": "B", "t": 2.0, "note": "upper-cased"},
    ]
    assert put.json() == {"markers": expected}
    got = client.get(f"/api/captures/{STEM}/annotations")
    assert got.status_code == 200
    assert got.json() == {"markers": expected}


def test_annotations_unknown_stem_is_404(client: TestClient) -> None:
    assert client.get(f"/api/captures/{UNKNOWN_STEM}/annotations").status_code == 404
    put = client.put(f"/api/captures/{UNKNOWN_STEM}/annotations", json={"markers": []})
    assert put.status_code == 404


# -- power analysis ---------------------------------------------------------------------------


def test_power_roundtrip(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    state = {"mode": "dc-dc", "rails": [{"v": "Voltage", "i": "Current"}]}
    put = client.put(f"/api/captures/{STEM}/power", json=state)
    assert put.status_code == 200
    assert put.json() == state
    got = client.get(f"/api/captures/{STEM}/power")
    assert got.status_code == 200
    assert got.json() == {**state, "source": "capture"}


def test_power_unknown_stem_is_404(client: TestClient) -> None:
    assert client.get(f"/api/captures/{UNKNOWN_STEM}/power").status_code == 404
    put = client.put(f"/api/captures/{UNKNOWN_STEM}/power", json={"mode": "battery", "rails": []})
    assert put.status_code == 404


def test_power_default_mode(client: TestClient, web_library: CaptureLibrary) -> None:
    response = client.put("/api/power/default", json={"mode": "sleep"})
    assert response.status_code == 200
    assert response.json() == {"mode": "sleep"}
    assert web_library.get_setting("power_mode") == "sleep"


def test_power_default_mode_invalid_falls_back(client: TestClient) -> None:
    response = client.put("/api/power/default", json={"mode": "nonsense"})
    assert response.status_code == 200
    assert response.json() == {"mode": "battery"}


# -- assertions ---------------------------------------------------------------------------------


def test_assertions_roundtrip_cleans_entries(client: TestClient) -> None:
    put = client.put(
        "/api/assertions",
        json={
            "assertions": [
                {"name": "Voltage", "min": 3.6, "max": 3.0},  # swapped bounds reordered
                {"name": "   ", "min": 1.0},  # blank name dropped
                {"name": "Current", "min": None, "max": None},  # no bounds dropped
            ]
        },
    )
    assert put.status_code == 200
    expected = [{"name": "Voltage", "min": 3.0, "max": 3.6}]
    assert put.json() == {"assertions": expected}
    assert client.get("/api/assertions").json() == {"assertions": expected}


def test_capture_assertions_evaluation(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    client.put(
        "/api/assertions",
        json={
            "assertions": [
                {"name": "Voltage", "min": 0.5, "max": 2.0},  # actual 1.0..1.1 -> pass
                {"name": "Current", "min": 3.0},  # actual 2.0..2.2 -> fail
            ]
        },
    )
    response = client.get(f"/api/captures/{STEM}/assertions")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"] == 2
    assert body["passed"] == 1
    by_name = {r["name"]: r for r in body["results"]}
    assert by_name["Voltage"]["pass"] is True
    assert by_name["Current"]["pass"] is False


def test_capture_assertions_unknown_stem_is_404(client: TestClient) -> None:
    response = client.get(f"/api/captures/{UNKNOWN_STEM}/assertions")
    assert response.status_code == 404


# -- report ------------------------------------------------------------------------------------


def test_report_creates_html_next_to_csv(client: TestClient, captures_dir: Path) -> None:
    csv_path = _write_capture(captures_dir)
    response = client.post(
        f"/api/captures/{STEM}/report",
        json={
            "lo": 1.0,
            "hi": 0.0,  # swapped on purpose: the route reorders them
            "zoom": {"lo": 0.9, "hi": 0.1},
            "power": {"mode": "battery", "rails": [{"v": "Voltage", "i": "Current"}]},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stem"] == STEM
    assert body["name"] == f"{STEM}.html"
    out = csv_path.with_suffix(".html")
    assert Path(body["path"]) == out
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Voltage" in html

    # The freshly generated report is served back with the sandbox CSP.
    served = client.get(f"/api/captures/{STEM}/file", params={"ext": "html"})
    assert served.status_code == 200
    assert served.headers["content-security-policy"] == "sandbox allow-scripts"


def test_report_unknown_stem_is_404(client: TestClient) -> None:
    response = client.post(f"/api/captures/{UNKNOWN_STEM}/report", json={})
    assert response.status_code == 404


def test_report_escapes_hostile_channel_name(client: TestClient, captures_dir: Path) -> None:
    buffer = io.StringIO()
    writer = csv_module.writer(buffer, lineterminator="\n")
    writer.writerow(["timestamp", "elapsed_s", "actual_sps", "counter", "averaged_n", HOSTILE])
    hostile_csv = (
        "# profile: default\n"
        f"# note: {HOSTILE}\n"
        f"# A0: {HOSTILE} (V)\n"
        + buffer.getvalue()
        + "2026-09-14T12:00:00.000000,0.0,0.0,0,0,1.0\n"
        + "2026-09-14T12:00:01.000000,1.0,1.0,1,0,1.1\n"
    )
    _write_capture(captures_dir, text=hostile_csv)
    response = client.post(f"/api/captures/{STEM}/report", json={})
    assert response.status_code == 200
    html = (captures_dir / f"{STEM}.html").read_text(encoding="utf-8")
    # The hostile name flows into the report, but never verbatim: no raw double
    # quote can break out of an attribute.
    assert HOSTILE not in html
    assert "&quot; onmouseover=&quot;alert(1)" in html


# -- project assignment / trash / apply-config ------------------------------------------------


def test_capture_project_unknown_stem_is_404(client: TestClient) -> None:
    response = client.post(f"/api/captures/{UNKNOWN_STEM}/project", json={"project": None})
    assert response.status_code == 404


def test_capture_project_assign_and_unknown_project(client: TestClient, captures_dir: Path) -> None:
    _write_capture(captures_dir)
    assert client.post("/api/projects", json={"name": "battery"}).status_code == 200
    ok = client.post(f"/api/captures/{STEM}/project", json={"project": "battery"})
    assert ok.status_code == 200
    assert ok.json() == {"stem": STEM, "project": "battery"}
    bad = client.post(f"/api/captures/{STEM}/project", json={"project": "nope"})
    assert bad.status_code == 400


def test_trash_moves_files(
    client: TestClient, captures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capture(captures_dir)
    (captures_dir / f"{STEM}.png").write_bytes(b"\x89PNG-fake")

    def fake_send2trash(path: str) -> None:
        Path(path).unlink()

    monkeypatch.setattr("benchweave.web.library.send2trash", fake_send2trash)
    response = client.post("/api/captures/trash", json={"stems": [STEM]})
    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert {t["name"] for t in body["trashed"]} == {f"{STEM}.csv", f"{STEM}.png"}
    assert not (captures_dir / f"{STEM}.csv").exists()


def test_trash_empty_stems_is_422(client: TestClient) -> None:
    response = client.post("/api/captures/trash", json={"stems": []})
    assert response.status_code == 422


def test_apply_config_uses_embedded_config(
    client: TestClient, captures_dir: Path, fake_manager: FakeBoardManager
) -> None:
    profile = copy.deepcopy(DEFAULT_CONFIG["profiles"]["default"])
    payload = {
        "active_profile": "default",
        "profile": profile,
        "settings": {"sample_rate_hz": 50.0},
    }
    text = (
        "# config: " + json.dumps(payload, separators=(",", ":")) + "\n"
        "timestamp,elapsed_s,actual_sps,counter,averaged_n\n"
    )
    _write_capture(captures_dir, text=text)
    response = client.post(f"/api/captures/{STEM}/apply-config")
    assert response.status_code == 200
    applied = response.json()
    assert applied["active_profile"] == "default"
    assert applied["settings"] == {"sample_rate_hz": 50.0}
    assert fake_manager.called("set_config") == [(applied,)]


def test_apply_config_reconstructs_when_no_embedded_config(
    client: TestClient, captures_dir: Path, fake_manager: FakeBoardManager
) -> None:
    # Pins current behaviour: a CSV without a `# config:` line still yields a
    # config (reconstructed from the channel descriptors over the defaults),
    # so the route applies it. The route's "no config metadata" 400 only fires
    # when config_from_csv returns None — which the current library never does.
    _write_capture(captures_dir)
    response = client.post(f"/api/captures/{STEM}/apply-config")
    assert response.status_code == 200
    applied = response.json()
    assert applied["profiles"]["default"]["channels"]["A0"]["name"] == "Voltage"
    assert len(fake_manager.called("set_config")) == 1


def test_apply_config_none_config_is_400(
    client: TestClient,
    captures_dir: Path,
    web_library: CaptureLibrary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_capture(captures_dir)

    def no_config(path: Path | str) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(web_library, "config_from_csv", no_config)
    response = client.post(f"/api/captures/{STEM}/apply-config")
    assert response.status_code == 400
    assert "no config metadata" in response.json()["detail"]


def test_apply_config_invalid_embedded_config_is_400(
    client: TestClient, captures_dir: Path, fake_manager: FakeBoardManager
) -> None:
    text = (
        '# config: {"active_profile":"broken","profile":{},"settings":{}}\n'
        "timestamp,elapsed_s,actual_sps,counter,averaged_n\n"
    )
    _write_capture(captures_dir, text=text)
    response = client.post(f"/api/captures/{STEM}/apply-config")
    assert response.status_code == 400
    assert fake_manager.called("set_config") == []


def test_apply_config_missing_csv_is_404(client: TestClient) -> None:
    response = client.post(f"/api/captures/{UNKNOWN_STEM}/apply-config")
    assert response.status_code == 404


# -- projects ----------------------------------------------------------------------------------


def test_projects_crud_via_api(client: TestClient) -> None:
    assert client.get("/api/projects").json() == []

    created = client.post("/api/projects", json={"name": "bench", "retention_days": 30})
    assert created.status_code == 200
    assert created.json() == {"name": "bench", "retention_days": 30}
    assert client.get("/api/projects").json() == [{"name": "bench", "retention_days": 30}]

    duplicate = client.post("/api/projects", json={"name": "bench"})
    assert duplicate.status_code == 400

    patched = client.patch("/api/projects/bench", json={"retention_days": 60})
    assert patched.status_code == 200
    assert patched.json() == {"name": "bench", "retention_days": 60}

    unknown = client.patch("/api/projects/ghost", json={"retention_days": 1})
    assert unknown.status_code == 404


def test_create_project_blank_name_is_400(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "   "})
    assert response.status_code == 400
