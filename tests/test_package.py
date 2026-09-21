from pytest import CaptureFixture

from benchweave import __version__
from benchweave.__main__ import main


def test_version_is_available() -> None:
    assert __version__


def test_cli_entrypoint_prints_version(capsys: CaptureFixture[str]) -> None:
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("benchweave-adc ")
