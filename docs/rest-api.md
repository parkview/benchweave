# REST API

The web frontend's HTTP API, served by `src/benchweave/web/app.py`. The live,
authoritative OpenAPI documentation is at **`/docs`** (Swagger UI) and
**`/openapi.json`** while the server runs — every route description below is
also there, generated from the handlers' docstrings.

Global behaviour: requests are unauthenticated (single operator on
localhost — see the README's security posture), and any request body over
20 MiB is rejected with **413**. `{stem}` is a capture's filename stem, e.g.
`adc_1234_20260917_101500`. The static UI is mounted at `/`.

## Board and configuration

| Method | Path | Purpose | Notable codes |
|---|---|---|---|
| GET | `/api/boards` | Probe serial ports, list discovered ADC boards | |
| POST | `/api/connect` | Connect by device path; restores saved channels | 400 open/identify failed |
| GET | `/api/status` | Connection, firmware, averaging, stream/record state | |
| GET | `/api/config` | Full runtime config (profiles + settings) | |
| PUT | `/api/config` | Replace and persist the config (validated) | 422 invalid config |
| POST | `/api/averaging` | Set hardware averaging depth | 422 unsupported value, 400 not idle |
| POST | `/api/channels` | Set enabled-channel bitmask, persisted | 422 outside 0..63, 400 not idle |

## Streaming and live graph

| Method | Path | Purpose | Notable codes |
|---|---|---|---|
| POST | `/api/stream/start` | Start streaming; `record` opens a CSV with `note` | 400 not connected |
| POST | `/api/stream/stop` | Stop streaming, close any CSV (idempotent) | |
| POST | `/api/stream/pause` | Halt collection, keep the CSV open | 400 not connected |
| POST | `/api/stream/resume` | Resume into the same CSV, elapsed time continuous | 400 not connected |
| GET | `/api/stream` | SSE feed of converted samples, ≤ ~30 Hz per client | 503 subscriber cap (32) |
| POST | `/api/graph/export` | Save a base64 PNG of the graph to the captures dir | 422 bad base64, 413 image > 10 MiB |
| POST | `/api/graph/reveal` | Open the OS file manager at the last exported PNG | 500 no file manager |

## Capture library (Analyse tab)

| Method | Path | Purpose | Notable codes |
|---|---|---|---|
| GET | `/api/captures` | List capture files, newest first, with project/retention | |
| GET | `/api/captures/storage` | Total bytes and per-project breakdown | |
| GET | `/api/captures/retention` | Stems past their retention date (advisory) | |
| GET | `/api/captures/{stem}/data` | Parsed CSV series; `max_points` decimates (default 5000, 0 = all) | 404 no CSV, 400 parse failure |
| GET | `/api/captures/{stem}/file` | Serve the csv/png/html file; HTML gets a sandbox CSP | 422 bad ext, 404 missing |
| POST | `/api/captures/{stem}/report` | Write a self-contained HTML report as `{stem}.html` | 404 no CSV |
| POST | `/api/captures/{stem}/project` | Assign to a project (null clears) | 404 no capture, 400 unknown project |
| POST | `/api/captures/trash` | Move stems' files to the OS trash; drop orphaned metadata | 422 empty list |
| POST | `/api/captures/{stem}/apply-config` | Push the capture's recorded profile to the board | 404 no CSV, 400 no config/apply failed |

## Annotations, power analysis, assertions

| Method | Path | Purpose | Notable codes |
|---|---|---|---|
| GET | `/api/captures/{stem}/annotations` | The capture's A–Z letter markers | 404 no CSV |
| PUT | `/api/captures/{stem}/annotations` | Replace the markers (validated) | 404 no CSV |
| GET | `/api/captures/{stem}/power` | Power setup; falls back to reused/default (`source` says which) | 404 no CSV |
| PUT | `/api/captures/{stem}/power` | Save the capture's power setup (mode + V/I rails) | 404 no CSV |
| PUT | `/api/power/default` | Set the global default power mode | |
| GET | `/api/assertions` | The global per-channel min/max assertions | |
| PUT | `/api/assertions` | Replace the assertions (validated) | |
| GET | `/api/captures/{stem}/assertions` | Evaluate the assertions against one capture | 404 no CSV |

## Projects

| Method | Path | Purpose | Notable codes |
|---|---|---|---|
| GET | `/api/projects` | List projects with retention days | |
| POST | `/api/projects` | Create a project (null retention inherits the global default) | 400 empty/duplicate name |
| PATCH | `/api/projects/{name}` | Change a project's retention | 404 unknown project |

The metadata behind projects, annotations, power setups, and assertions is
described in [library-schema.md](library-schema.md).
