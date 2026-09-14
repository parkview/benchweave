import json
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from benchweave.web.library import CaptureLibrary

STEM = "adc_1234_test_20260914_120000"

CSV = (
    "# profile: default\n"
    "# note: test capture\n"
    "# sample_rate_hz: 100.0\n"
    "# A0: Voltage (V)\n"
    "# A1: Current (A)\n"
    "# computed: Power (W) = A0*A1\n"
    "timestamp,elapsed_s,actual_sps,counter,averaged_n,Voltage,Current,Power\n"
    "2026-09-14T12:00:00.000000,0.0,0.0,0,0,1.0,2.0,2.0\n"
    "2026-09-14T12:00:01.000000,1.0,1.0,1,0,1.1,2.2,2.42\n"
)


@pytest.fixture
def library(tmp_path: Path) -> CaptureLibrary:
    (tmp_path / f"{STEM}.csv").write_text(CSV)
    (tmp_path / f"{STEM}.png").write_bytes(b"\x89PNG-fake")
    return CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")


def test_scan_lists_files_and_groups_by_stem(library: CaptureLibrary) -> None:
    records = library.scan()
    by_kind = {r["kind"] for r in records}
    assert by_kind == {"csv", "png"}

    csv_rec = next(r for r in records if r["kind"] == "csv")
    assert csv_rec["serial"] == "1234"
    assert csv_rec["tag"] == "test"
    assert csv_rec["size_bytes"] == Path(csv_rec["path"]).stat().st_size
    assert csv_rec["expired"] is False  # retention 7 days, captured today


def test_scan_assigns_project_and_retention(library: CaptureLibrary) -> None:
    library.create_project("battery", retention_days=0)
    library.assign_project(STEM, "battery")

    records = library.scan()
    assert all(r["project"] == "battery" for r in records)
    assert all(r["expired"] is True for r in records)  # retention 0 -> immediately expired
    assert all(r["retention_days"] == 0 for r in records)


def test_expired_math() -> None:
    now = datetime(2026, 9, 14, 12, 0, 0)
    captured = datetime(2026, 9, 10, 12, 0, 0)
    assert CaptureLibrary._expired(captured, 4, now) is True
    assert CaptureLibrary._expired(captured, 5, now) is False
    assert CaptureLibrary._expired(captured, None, now) is False


def test_parse_csv_reads_series_positionally(library: CaptureLibrary) -> None:
    data = library.parse_csv(library.file_for(STEM, "csv"))
    assert data["sample_count"] == 2
    assert data["duration_s"] == 1.0
    names = [s["name"] for s in data["series"]]
    assert names == ["Voltage", "Current", "Power"]
    # First series' points are [elapsed_s, value].
    assert data["series"][0]["points"] == [[0.0, 1.0], [1.0, 1.1]]


def test_parse_csv_handles_duplicate_display_names(tmp_path: Path) -> None:
    csv = (
        "# profile: default\n"
        "# A2: 5V (V)\n"
        "# computed: 5V (V) = A2*2\n"
        "timestamp,elapsed_s,actual_sps,counter,averaged_n,5V,5V\n"
        "2026-09-14T12:00:00.000000,0.0,0.0,0,0,3.3,6.6\n"
    )
    (tmp_path / f"{STEM}.csv").write_text(csv)
    lib = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")

    data = lib.parse_csv(lib.file_for(STEM, "csv"))
    # Positional parsing keeps both series despite the duplicate name.
    assert [s["name"] for s in data["series"]] == ["5V", "5V"]
    assert data["series"][0]["points"] == [[0.0, 3.3]]
    assert data["series"][1]["points"] == [[0.0, 6.6]]


def test_parse_csv_empty_file(tmp_path: Path) -> None:
    (tmp_path / f"{STEM}.csv").write_text("")
    lib = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")

    data = lib.parse_csv(lib.file_for(STEM, "csv"))
    assert data["sample_count"] == 0
    assert data["series"] == []


def test_storage_stats_totals_and_groups(library: CaptureLibrary) -> None:
    library.create_project("battery", retention_days=7)
    library.assign_project(STEM, "battery")

    stats = library.storage_stats()
    assert stats["file_count"] == 2
    assert stats["total_bytes"] > 0
    assert {p["project"] for p in stats["per_project"]} == {"battery"}
    assert sum(p["bytes"] for p in stats["per_project"]) == stats["total_bytes"]


def test_storage_stats_buckets_unassigned(library: CaptureLibrary) -> None:
    stats = library.storage_stats()
    assert {p["project"] for p in stats["per_project"]} == {"(unassigned)"}


def test_retention_scan_returns_expired_stems(library: CaptureLibrary) -> None:
    library.create_project("battery", retention_days=0)
    library.assign_project(STEM, "battery")
    assert library.retention_scan() == [STEM]


def test_trash_moves_files_and_drops_rows(library: CaptureLibrary) -> None:
    with (
        mock.patch("benchweave.web.library.shutil.which", return_value="gio"),
        mock.patch("benchweave.web.library.subprocess.run") as run,
    ):
        result = library.trash([STEM])

    assert result["errors"] == []
    assert {t["name"] for t in result["trashed"]} == {
        f"{STEM}.csv",
        f"{STEM}.png",
    }
    assert len(run.call_args_list) == 2
    assert all(args[0][0][:2] == ["gio", "trash"] for args in run.call_args_list)


def test_trash_no_tool_reports_error(library: CaptureLibrary) -> None:
    with mock.patch("benchweave.web.library.shutil.which", return_value=None):
        result = library.trash([STEM])

    assert result["trashed"] == []
    assert all("no trash tool" in e["detail"] for e in result["errors"])


def test_file_for_rejects_traversal(library: CaptureLibrary) -> None:
    assert library.file_for("../etc/passwd", "csv") is None
    assert library.file_for("a/b", "csv") is None
    assert library.file_for(STEM, "png") is not None
    assert library.file_for(STEM, "html") is None


def test_project_crud(library: CaptureLibrary) -> None:
    library.create_project("bench", retention_days=30)
    assert {p["name"] for p in library.list_projects()} == {"bench"}

    library.set_project_retention("bench", 60)
    assert library.list_projects()[0]["retention_days"] == 60

    with pytest.raises(ValueError, match="already exists"):
        library.create_project("bench")

    with pytest.raises(ValueError, match="unknown project"):
        library.set_project_retention("nope", 1)


def test_assign_unknown_project_raises(library: CaptureLibrary) -> None:
    with pytest.raises(ValueError, match="unknown project"):
        library.assign_project(STEM, "nope")


def test_config_from_csv_reconstructs_profile(library: CaptureLibrary) -> None:
    config = library.config_from_csv(library.file_for(STEM, "csv"))
    assert config is not None
    assert config["active_profile"] == "default"
    channels = config["profiles"]["default"]["channels"]
    assert channels["A0"]["name"] == "Voltage"
    assert channels["A0"]["unit"] == "V"
    assert channels["A1"]["unit"] == "A"
    computed = config["profiles"]["default"]["computed"]
    assert computed[0]["expr"] == "A0*A1"


def test_config_from_csv_reads_embedded_config(tmp_path: Path) -> None:
    # New captures embed the full profile as a compact single-line JSON
    # (the same shape board.py::_record_meta now writes).
    payload = {
        "active_profile": "bench",
        "profile": {"channels": {}, "computed": []},
        "settings": {"sample_rate_hz": 50.0},
    }
    csv = (
        "# config: " + json.dumps(payload, separators=(",", ":")) + "\n"
        "timestamp,elapsed_s,actual_sps,counter,averaged_n\n"
    )
    (tmp_path / f"{STEM}.csv").write_text(csv)
    lib = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")

    config = lib.config_from_csv(lib.file_for(STEM, "csv"))
    assert config["active_profile"] == "bench"
    assert config["profiles"]["bench"]["channels"] == {}
    assert config["settings"]["sample_rate_hz"] == 50.0


def test_annotations_empty_by_default(library: CaptureLibrary) -> None:
    assert library.get_annotations(STEM) == []


def test_annotations_roundtrip_and_sort_by_label(library: CaptureLibrary) -> None:
    markers = [
        {"label": "C", "t": 3.0, "note": "settled"},
        {"label": "A", "t": 1.0, "note": "power on"},
        {"label": "B", "t": 2.0, "note": "inrush"},
    ]
    stored = library.set_annotations(STEM, markers)
    assert [m["label"] for m in stored] == ["A", "B", "C"]
    assert library.get_annotations(STEM) == stored


def test_annotations_clean_invalid_markers(library: CaptureLibrary) -> None:
    stored = library.set_annotations(
        STEM,
        [
            {"label": "a", "t": 1.0, "note": "lowercase dropped"},
            {"label": "A", "t": 1.0, "note": "kept"},
            {"label": "A", "t": 9.0, "note": "duplicate dropped"},
            {"label": "1", "t": 2.0, "note": "non-letter dropped"},
            {"label": "B", "t": -1.0, "note": "negative t dropped"},
            {"label": "C", "t": "x", "note": "non-numeric t dropped"},
        ],
    )
    assert [m["label"] for m in stored] == ["A"]
    assert stored[0]["t"] == 1.0


def test_annotations_pruned_when_capture_deleted(library: CaptureLibrary) -> None:
    library.set_annotations(STEM, [{"label": "A", "t": 1.0, "note": ""}])
    assert library.get_annotations(STEM)  # saved

    (library._captures_dir / f"{STEM}.csv").unlink()
    (library._captures_dir / f"{STEM}.png").unlink()
    library.scan()  # reconcile drops the capture row -> cascade deletes annotations

    assert library.get_annotations(STEM) == []
