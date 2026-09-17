import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast
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
    assert csv_rec["size_bytes"] == Path(cast(str, csv_rec["path"])).stat().st_size
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
    path = library.file_for(STEM, "csv")
    assert path is not None
    data = cast(dict[str, Any], library.parse_csv(path))
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

    path = lib.file_for(STEM, "csv")
    assert path is not None
    data = cast(dict[str, Any], lib.parse_csv(path))
    # Positional parsing keeps both series despite the duplicate name.
    assert [s["name"] for s in data["series"]] == ["5V", "5V"]
    assert data["series"][0]["points"] == [[0.0, 3.3]]
    assert data["series"][1]["points"] == [[0.0, 6.6]]


def test_parse_csv_names_series_from_deduplicated_columns(tmp_path: Path) -> None:
    # A capture written after the duplicate-name fix carries unique column names
    # in the header, while the `#` metadata still records the raw labels. Series
    # are named by the (unique) columns, not the metadata.
    csv = (
        "# profile: default\n"
        "# A2: 5V (V)\n"
        "# computed: 5V (V) = A2*2\n"
        "timestamp,elapsed_s,actual_sps,counter,averaged_n,5V,5V (computed)\n"
        "2026-09-14T12:00:00.000000,0.0,0.0,0,0,3.3,6.6\n"
    )
    (tmp_path / f"{STEM}.csv").write_text(csv)
    lib = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")

    path = lib.file_for(STEM, "csv")
    assert path is not None
    data = cast(dict[str, Any], lib.parse_csv(path))
    assert [s["name"] for s in data["series"]] == ["5V", "5V (computed)"]
    assert [s["unit"] for s in data["series"]] == ["V", "V"]


def test_parse_csv_empty_file(tmp_path: Path) -> None:
    (tmp_path / f"{STEM}.csv").write_text("")
    lib = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")

    path = lib.file_for(STEM, "csv")
    assert path is not None
    data = cast(dict[str, Any], lib.parse_csv(path))
    assert data["sample_count"] == 0
    assert data["series"] == []


def test_storage_stats_totals_and_groups(library: CaptureLibrary) -> None:
    library.create_project("battery", retention_days=7)
    library.assign_project(STEM, "battery")

    stats = cast(dict[str, Any], library.storage_stats())
    assert stats["file_count"] == 2
    assert stats["total_bytes"] > 0
    assert {p["project"] for p in stats["per_project"]} == {"battery"}
    assert sum(p["bytes"] for p in stats["per_project"]) == stats["total_bytes"]


def test_storage_stats_buckets_unassigned(library: CaptureLibrary) -> None:
    stats = cast(dict[str, Any], library.storage_stats())
    assert {p["project"] for p in stats["per_project"]} == {"(unassigned)"}


def test_retention_scan_returns_expired_stems(library: CaptureLibrary) -> None:
    library.create_project("battery", retention_days=0)
    library.assign_project(STEM, "battery")
    assert library.retention_scan() == [STEM]


def test_trash_moves_files_and_drops_rows(library: CaptureLibrary) -> None:
    def fake_send2trash(path: str) -> None:
        Path(path).unlink()

    with mock.patch("benchweave.web.library.send2trash", side_effect=fake_send2trash) as sender:
        result = cast(dict[str, Any], library.trash([STEM]))

    assert result["errors"] == []
    assert {t["name"] for t in result["trashed"]} == {
        f"{STEM}.csv",
        f"{STEM}.png",
    }
    assert len(sender.call_args_list) == 2
    # With every file gone, the explicitly trashed stem's row is dropped.
    assert library.get_annotations(STEM) == []


def test_trash_failure_reports_error_and_keeps_metadata(library: CaptureLibrary) -> None:
    with mock.patch("benchweave.web.library.send2trash", side_effect=OSError("no trash here")):
        result = cast(dict[str, Any], library.trash([STEM]))

    assert result["trashed"] == []
    assert all("no trash here" in e["detail"] for e in result["errors"])
    # Files survived the failed trash, so the metadata row must survive too.
    assert library.file_for(STEM, "csv") is not None


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
    config = library.config_from_csv(cast(Path, library.file_for(STEM, "csv")))
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

    config = lib.config_from_csv(cast(Path, lib.file_for(STEM, "csv")))
    assert config is not None
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


def test_annotations_survive_missing_files(library: CaptureLibrary) -> None:
    library.set_annotations(STEM, [{"label": "A", "t": 1.0, "note": ""}])
    assert library.get_annotations(STEM)  # saved

    # A scan with the files absent marks the row missing but must never
    # cascade-delete the markers (the old behaviour silently wiped them).
    (library._captures_dir / f"{STEM}.csv").unlink()
    (library._captures_dir / f"{STEM}.png").unlink()
    library.scan()

    assert library.get_annotations(STEM) == [{"label": "A", "t": 1.0, "note": ""}]


def test_power_empty_by_default(library: CaptureLibrary) -> None:
    assert library.get_power(STEM) == {"mode": "battery", "rails": [], "source": "default"}


def test_power_roundtrip(library: CaptureLibrary) -> None:
    state: dict[str, object] = {
        "mode": "dc-dc",
        "rails": [
            {"v": "Voltage", "i": "Current"},
            {"v": "Voltage", "i": "Current"},
        ],
    }
    assert library.set_power(STEM, state) == state
    assert library.get_power(STEM) == {**state, "source": "capture"}


def test_power_clean_invalid(library: CaptureLibrary) -> None:
    state: dict[str, object] = {
        "mode": "nonsense",
        "rails": [
            {"v": "Voltage", "i": "Current"},
            "not-a-dict",
            {"v": "Voltage", "i": "Current"},
            {"v": "Voltage", "i": "Current"},
        ],
    }
    stored = library.set_power(STEM, state)
    assert stored["mode"] == "battery"  # invalid mode -> battery
    assert stored["rails"] == [  # non-dict dropped, list truncated to 2
        {"v": "Voltage", "i": "Current"},
        {"v": "Voltage", "i": "Current"},
    ]


def test_power_reuse_by_matching_names(library: CaptureLibrary) -> None:
    library.set_power(STEM, {"mode": "load-step", "rails": [{"v": "Voltage", "i": "Current"}]})

    stem2 = "adc_5678_test2_20260914_130000"
    (library._captures_dir / f"{stem2}.csv").write_text(CSV)

    got = library.get_power(stem2)
    assert got == {
        "mode": "load-step",
        "rails": [{"v": "Voltage", "i": "Current"}],
        "source": "reused",
    }


def test_power_reuse_skips_unmatched_names(library: CaptureLibrary) -> None:
    library.set_power(STEM, {"mode": "sleep", "rails": [{"v": "3V3", "i": "mA"}]})

    stem2 = "adc_5678_test2_20260914_130000"
    (library._captures_dir / f"{stem2}.csv").write_text(CSV)

    got = library.get_power(stem2)
    assert got == {"mode": "battery", "rails": [], "source": "default"}


def test_power_default_mode(library: CaptureLibrary) -> None:
    assert library.set_default_mode("sleep") == "sleep"
    assert library.get_setting("power_mode") == "sleep"
    assert library.get_power(STEM) == {"mode": "sleep", "rails": [], "source": "default"}


def test_power_default_mode_invalid_falls_back(library: CaptureLibrary) -> None:
    assert library.set_default_mode("nonsense") == "battery"
    assert library.get_setting("power_mode") == "battery"


def test_power_survives_missing_files_and_dies_on_trash(library: CaptureLibrary) -> None:
    library.set_power(STEM, {"mode": "sleep", "rails": [{"v": "Voltage", "i": "Current"}]})
    assert library.get_power(STEM)["source"] == "capture"

    # Files vanishing from disk (unmounted drive, sync client mid-flight) only
    # MARKS the row missing; the power analysis must survive the next scan.
    csv_bytes = (library._captures_dir / f"{STEM}.csv").read_bytes()
    (library._captures_dir / f"{STEM}.csv").unlink()
    (library._captures_dir / f"{STEM}.png").unlink()
    library.scan()
    assert library.get_power(STEM)["source"] == "capture"

    # And when the files come back, everything reads exactly as before.
    (library._captures_dir / f"{STEM}.csv").write_bytes(csv_bytes)
    library.scan()
    assert library.get_power(STEM)["source"] == "capture"

    # Only the explicit trash API hard-deletes the row (and cascades power).
    (library._captures_dir / f"{STEM}.csv").unlink()
    library.trash([STEM])
    assert library.get_power(STEM) == {"mode": "battery", "rails": [], "source": "default"}


def test_assertions_empty_by_default(library: CaptureLibrary) -> None:
    assert library.get_assertions() == []


def test_assertions_roundtrip_and_clean(library: CaptureLibrary) -> None:
    items = cast(
        list[dict[str, object]],
        [
            {"name": "Voltage", "min": 3.0, "max": 3.6},
            {"name": "Current", "min": 0.0},
            {"name": "   ", "min": 1.0},  # blank name dropped
            {"name": "Power", "min": None, "max": None},  # no bounds dropped
            {"name": "Current", "min": 9.0},  # duplicate name dropped
            "not-a-dict",  # skipped
        ],
    )
    stored = library.set_assertions(items)
    assert stored == [
        {"name": "Voltage", "min": 3.0, "max": 3.6},
        {"name": "Current", "min": 0.0, "max": None},
    ]
    assert library.get_assertions() == stored


def test_check_assertions_pass_and_fail(library: CaptureLibrary) -> None:
    library.set_assertions(
        [
            {"name": "Voltage", "min": 0.5, "max": 2.0},  # actual 1.0..1.1 -> pass
            {"name": "Current", "min": 3.0},  # actual 2.0..2.2 -> fail
            {"name": "Missing", "max": 1.0},  # no such channel -> fail
        ]
    )
    res = cast(dict[str, Any], library.check_assertions(STEM))
    assert res["checks"] == 3
    assert res["passed"] == 1

    by_name = {r["name"]: r for r in res["results"]}
    assert by_name["Voltage"]["pass"] is True
    assert by_name["Voltage"]["actual_min"] == 1.0
    assert by_name["Voltage"]["actual_max"] == 1.1
    assert by_name["Voltage"]["unit"] == "V"
    assert by_name["Current"]["pass"] is False
    assert by_name["Missing"]["found"] is False


def test_check_assertions_without_csv(library: CaptureLibrary) -> None:
    library.set_assertions([{"name": "Voltage", "min": 0.5}])
    res = library.check_assertions("adc_nope_20260914_120000")
    assert res["results"] == [
        {
            "name": "Voltage",
            "min": 0.5,
            "max": None,
            "actual_min": None,
            "actual_max": None,
            "unit": "",
            "found": False,
            "pass": False,
        }
    ]
