# Capture library schema

The capture library (`src/benchweave/web/library.py`) overlays metadata on
the capture files with a small SQLite database at
`plugins/adc_6ch_12bit/library.db` (beside the `captures/` directory;
gitignored).

**The filesystem is the source of truth for what exists.** The database
never decides which captures are real — it only remembers things about them:
project assignment, retention, annotations, power-analysis setups, and a few
settings. A CSV and the PNG/HTML derived from it share a filename *stem* and
are treated as one capture.

## Concurrency posture

The web app and the MCP server are separate processes sharing this file, so
the database runs in **WAL mode** (readers no longer block a writer
wholesale) with a **5 s connection timeout and `busy_timeout`** — a
concurrent write waits instead of surfacing an immediate "database is
locked" error. Foreign keys are enforced on every connection.

## Tables

| Table | Columns | Notes |
|---|---|---|
| `projects` | `name` PK, `retention_days` | NULL retention inherits the global `settings.retention_days` from the plugin config |
| `captures` | `stem` PK, `project` FK → projects (ON DELETE SET NULL), `assigned_at`, `missing_since` | One row per stem; created on demand by scan/assign/annotate |
| `annotations` | `stem` PK FK → captures (ON DELETE CASCADE), `markers` | JSON list of A–Z letter markers (`label`, `t`, `note`) |
| `power_analysis` | `stem` PK FK → captures (ON DELETE CASCADE), `json`, `saved_at` | JSON power setup: mode + up to two V/I rails; `saved_at` orders the reuse fallback |
| `settings` | `key` PK, `value` | String key/value: `power_mode` (default mode), `assertions` (the global checks, JSON) |

Schema changes are additive: `missing_since` was added with an
`ALTER TABLE` migration that runs on startup when the column is absent.

## The `missing_since` lifecycle (soft delete)

Every `scan()` reconciles rows with the files on disk:

1. Stems present on disk get a row if they lack one, and their
   `missing_since` is cleared.
2. Stems whose files are all absent are **marked**, not deleted:
   `missing_since` is stamped with the current time (only if not already
   set, so the stamp records when the files first went missing).

Rows are never deleted by reconciliation. Annotations and power setups hang
off `captures` via ON DELETE CASCADE, and a temporarily unmounted drive, a
sync client mid-flight, or an emptied captures directory must not wipe them —
the metadata is waiting when the files come back.

**Hard deletion happens in exactly one place**: the `trash()` API (behind
`POST /api/captures/trash`). It moves the stems' files to the operating
system trash — never a hard file delete — and then removes the `captures`
rows (cascading to annotations and power setups) only for stems the user
explicitly trashed *and* that have no file of any kind left on disk.

## Retention

Retention is derived, not stored per capture: a capture's retention is its
project's `retention_days`, or the global default for unassigned captures.
Expiry is computed against the capture time parsed from the filename stem
(`adc_<serial>[_<tag>]_<YYYYmmdd_HHMMSS>`), falling back to the file's
mtime. Expired captures are only *suggested* for trashing — see the
[Analyse page guide](analyse-page.md).
