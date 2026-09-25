"""Shared fixtures for the web API tests: data-dir seam, fresh library, fake manager.

``benchweave.web.app`` instantiates a real ``BoardManager`` and
``CaptureLibrary`` at import time, and the library's default paths come from
``capture_dir()``. The fixtures below point the plugin's data root at a
per-test tmp directory (via ``BENCHWEAVE_ADC_DATA_DIR``) before the app module
is imported, then swap in a fresh ``CaptureLibrary`` on those tmp dirs and a
``FakeBoardManager``, so no route ever touches real hardware or the repo tree.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from benchweave.web.library import CaptureLibrary
from tests.web.fakes import FakeBoardManager


@pytest.fixture
def fake_manager() -> FakeBoardManager:
    return FakeBoardManager()


@pytest.fixture
def captures_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``BENCHWEAVE_ADC_DATA_DIR`` at ``tmp_path``; return the captures dir."""
    monkeypatch.setenv("BENCHWEAVE_ADC_DATA_DIR", str(tmp_path))
    captures = tmp_path / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    return captures


@pytest.fixture
def web_library(captures_dir: Path) -> CaptureLibrary:
    """A fresh ``CaptureLibrary`` rooted in the per-test tmp data dir."""
    return CaptureLibrary(db_path=captures_dir.parent / "library.db", captures_dir=captures_dir)


@pytest.fixture
def client(
    captures_dir: Path,
    web_library: CaptureLibrary,
    fake_manager: FakeBoardManager,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """A ``TestClient`` over the app, with the library and manager swapped out.

    The app module is imported here — after ``BENCHWEAVE_ADC_DATA_DIR`` points
    at the tmp dir — because importing it instantiates a ``CaptureLibrary``,
    which captures ``capture_dir()`` at construction time. The ``with`` block
    runs the app lifespan (``set_loop`` on enter, ``disconnect`` on exit),
    which the fake manager accepts.
    """
    from benchweave.web import app as web_app

    monkeypatch.setattr(web_app, "library", web_library)
    monkeypatch.setattr(web_app, "manager", fake_manager)
    with TestClient(web_app.app) as test_client:
        yield test_client
