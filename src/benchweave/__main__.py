"""BenchWeave command line entrypoint."""

from benchweave import __version__


def main() -> int:
    """Print the installed BenchWeave version."""
    print(f"benchweave-adc {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
