# Development and CI

Use Python 3.13 (as pinned in `.python-version`) and uv. CI pins uv 0.11.16.
From the project root:

```sh
uv python install
uv sync --locked --dev
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest
uv run --no-sync benchweave-adc
uv build
```

Add dependencies with `uv add` or `uv add --dev`, and commit both
`pyproject.toml` and `uv.lock`. CI rejects a stale lockfile. Build dependencies
are resolved separately using the build-system requirements in `pyproject.toml`.

## GitHub workflows

- **CI** checks lint, formatting and types on Linux, and tests the pinned Python
  version on Linux, macOS and Windows.
- **Package** builds an sdist and wheel, installs the wheel into an isolated
  environment, and checks the CLI and typing marker outside the checkout.

Both workflows run on pushes, pull requests and manual dispatch. They use
read-only repository permissions, pinned action commits, timeouts and
cancellation of superseded runs. They do not need repository secrets.
The uv integration follows the [official uv GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/).

These checks do not establish runtime conformance or hardware qualification.
Hosted runs require the repository to be pushed to GitHub with Actions enabled.

## Documentation baseline

`docs/` carries this project's own documentation (the Analyse page guide and
feature notes); the hardware and protocol reference lives beside the plugin at
`plugins/adc_6ch_12bit/README.md`, and the firmware under
`firmware/ch32v006e8r_adc/`. The upstream BenchWeave architecture corpus this
repository once carried now lives in the upstream project; use Git history to
consult superseded revisions.
