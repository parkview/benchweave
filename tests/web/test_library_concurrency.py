"""Concurrency smoke: two writers on one library database (WAL + busy timeout)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fakes import CSV, STEM

from benchweave.web.library import CaptureLibrary

ITERATIONS = 100


@pytest.mark.timeout(120)
def test_two_writers_share_one_database(tmp_path: Path) -> None:
    """Two CaptureLibrary instances (separate connections, like the web app and
    the MCP server) hammer scan() + set_annotations() on one database. The WAL
    journal and the 5 s busy timeout must absorb the contention: no writer may
    ever surface 'database is locked'."""
    (tmp_path / f"{STEM}.csv").write_text(CSV, encoding="utf-8")
    first = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")
    second = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def hammer(library: CaptureLibrary, label: str) -> None:
        barrier.wait()
        try:
            for i in range(ITERATIONS):
                library.scan()
                library.set_annotations(
                    STEM, [{"label": label, "t": float(i), "note": "spin"}]
                )
        except Exception as exc:  # pragma: no cover - reported via the assert
            errors.append(exc)

    threads = [
        threading.Thread(target=hammer, args=(first, "A")),
        threading.Thread(target=hammer, args=(second, "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90.0)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    # set_annotations replaces the marker list wholesale, so whichever writer
    # landed last left exactly one valid marker behind.
    markers = first.get_annotations(STEM)
    assert len(markers) == 1
    assert markers[0]["label"] in {"A", "B"}
