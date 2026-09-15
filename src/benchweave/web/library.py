"""Capture library: browse, group, retain, and re-analyse captured ADC files.

Captured CSV/PNG/HTML files live on disk under :func:`capture_dir` (the
filesystem is the source of truth for *what exists*). A small SQLite database
overlays project assignment and per-project retention. A CSV and its derived
PNG/HTML share a filename *stem*, so they are grouped and retained together.
"""

from __future__ import annotations

import contextlib
import csv
import json
import math
import re
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from plugins.adc_6ch_12bit.config import CHANNEL_KEYS, DEFAULT_CONFIG, PALETTE, load_config
from plugins.adc_6ch_12bit.discovery import capture_dir

# adc_<serial>[_<tag>]_<YYYYmmdd_HHMMSS>.<ext>
_STEM_RE = re.compile(
    r"^adc_(?P<serial>[^_]+?)(?:_(?P<tag>[^_]+))?_(?P<stamp>\d{8}_\d{6})$"
)
_PHYSICAL_LINE = re.compile(r"^(.*?)\s*\(([^)]*)\)$")
_COMPUTED_LINE = re.compile(r"^(.*?)\s*\(([^)]*)\)\s*=\s*(.*)$")
_FILE_SUFFIXES = (".csv", ".png", ".html")
_POWER_MODES = ("battery", "dc-dc", "sleep", "load-step")


class CaptureLibrary:
    """Browse and manage captured ADC files with a SQLite metadata overlay."""

    def __init__(self, db_path: Path | None = None, captures_dir: Path | None = None) -> None:
        self._captures_dir = captures_dir if captures_dir is not None else capture_dir()
        self._db_path = (
            db_path if db_path is not None else self._captures_dir.parent / "library.db"
        )
        self._lock = threading.Lock()
        self._init_db()

    # -- storage -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with self._lock, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS projects ("
                    "name TEXT PRIMARY KEY, retention_days INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS captures ("
                    "stem TEXT PRIMARY KEY, "
                    "project TEXT REFERENCES projects(name) ON DELETE SET NULL, "
                    "assigned_at TEXT)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS annotations ("
                    "stem TEXT PRIMARY KEY REFERENCES captures(stem) ON DELETE CASCADE, "
                    "markers TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS power_analysis ("
                    "stem TEXT PRIMARY KEY REFERENCES captures(stem) ON DELETE CASCADE, "
                    "json TEXT NOT NULL, saved_at TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS settings ("
                    "key TEXT PRIMARY KEY, value TEXT)"
                )
        finally:
            conn.close()

    def file_for(self, stem: str, kind: str = "csv") -> Path | None:
        """Resolve a capture stem to an on-disk file, guarding path traversal."""
        if not re.fullmatch(r"[A-Za-z0-9._-]+", stem):
            return None
        path = self._captures_dir / f"{stem}.{kind}"
        return path if path.is_file() else None

    def _iter_files(self) -> list[Path]:
        if not self._captures_dir.exists():
            return []
        return sorted(
            p
            for p in self._captures_dir.iterdir()
            if p.is_file() and p.suffix in _FILE_SUFFIXES
        )

    @staticmethod
    def _parse_stem(stem: str) -> tuple[str | None, str | None, datetime | None]:
        """Return (serial, tag, captured_at) for a capture stem, or Nones."""
        match = _STEM_RE.match(stem)
        if not match:
            return None, None, None
        try:
            stamp = datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S")
        except ValueError:
            return match.group("serial"), match.group("tag"), None
        return match.group("serial"), match.group("tag"), stamp

    def _reconcile(self, conn: sqlite3.Connection, stems: set[str]) -> None:
        with conn:
            for stem in stems:
                conn.execute(
                    "INSERT OR IGNORE INTO captures (stem, project) VALUES (?, NULL)", (stem,)
                )
            conn.execute(
                "DELETE FROM captures WHERE stem NOT IN ("
                + ",".join("?" for _ in stems)
                + ")",
                tuple(stems),
            )

    # -- projects ------------------------------------------------------------

    def list_projects(self) -> list[dict[str, object]]:
        conn = self._connect()
        try:
            with self._lock, conn:
                rows = conn.execute(
                    "SELECT name, retention_days FROM projects ORDER BY name"
                ).fetchall()
            return [
                {"name": r["name"], "retention_days": r["retention_days"]} for r in rows
            ]
        finally:
            conn.close()

    def create_project(self, name: str, retention_days: int | None = None) -> dict[str, object]:
        name = name.strip()
        if not name:
            raise ValueError("project name is required")
        conn = self._connect()
        try:
            with self._lock, conn:
                conn.execute(
                    "INSERT INTO projects (name, retention_days) VALUES (?, ?)",
                    (name, retention_days),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"project '{name}' already exists") from exc
        finally:
            conn.close()
        return {"name": name, "retention_days": retention_days}

    def set_project_retention(self, name: str, retention_days: int | None) -> dict[str, object]:
        conn = self._connect()
        try:
            with self._lock, conn:
                cur = conn.execute(
                    "UPDATE projects SET retention_days = ? WHERE name = ?",
                    (retention_days, name),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"unknown project '{name}'")
        finally:
            conn.close()
        return {"name": name, "retention_days": retention_days}

    def assign_project(self, stem: str, project: str | None) -> dict[str, object]:
        conn = self._connect()
        try:
            with self._lock, conn:
                if project is not None:
                    row = conn.execute(
                        "SELECT 1 FROM projects WHERE name = ?", (project,)
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"unknown project '{project}'")
                conn.execute(
                    "INSERT INTO captures (stem, project, assigned_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(stem) DO UPDATE SET "
                    "project = excluded.project, assigned_at = excluded.assigned_at",
                    (stem, project, datetime.now().isoformat(timespec="seconds")),
                )
        finally:
            conn.close()
        return {"stem": stem, "project": project}

    # -- annotations ---------------------------------------------------------

    def get_annotations(self, stem: str) -> list[dict[str, object]]:
        """Return the letter markers saved for a capture (empty if none)."""
        conn = self._connect()
        try:
            with self._lock, conn:
                row = conn.execute(
                    "SELECT markers FROM annotations WHERE stem = ?", (stem,)
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            return []
        try:
            markers = json.loads(row["markers"])
        except (json.JSONDecodeError, TypeError):
            return []
        return cast(list[dict[str, object]], markers)

    def set_annotations(
        self, stem: str, markers: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Replace the capture's markers with the given (validated) list."""
        cleaned = self._clean_markers(markers)
        conn = self._connect()
        try:
            with self._lock, conn:
                conn.execute(
                    "INSERT OR IGNORE INTO captures (stem, project) VALUES (?, NULL)", (stem,)
                )
                conn.execute(
                    "INSERT INTO annotations (stem, markers) VALUES (?, ?) "
                    "ON CONFLICT(stem) DO UPDATE SET markers = excluded.markers",
                    (stem, json.dumps(cleaned)),
                )
        finally:
            conn.close()
        return cleaned

    @staticmethod
    def _clean_markers(
        markers: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Keep valid, unique single-letter markers; sort them by label."""
        seen: set[str] = set()
        out: list[dict[str, object]] = []
        for m in markers:
            label = str(m.get("label", "")).strip().upper()
            raw_t = m.get("t")
            if not re.fullmatch(r"[A-Z]", label) or label in seen:
                continue
            if not isinstance(raw_t, (int, float)) or not math.isfinite(float(raw_t)):
                continue
            t = float(raw_t)
            if t < 0:
                continue
            seen.add(label)
            out.append({"label": label, "t": t, "note": str(m.get("note", ""))})
        out.sort(key=lambda m: str(m["label"]))
        return out

    # -- power analysis ------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        conn = self._connect()
        try:
            with self._lock, conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
        finally:
            conn.close()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            with self._lock, conn:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        finally:
            conn.close()

    def set_default_mode(self, mode: str) -> str:
        if mode not in _POWER_MODES:
            mode = "battery"
        self.set_setting("power_mode", mode)
        return mode

    @staticmethod
    def _clean_power(state: dict[str, object]) -> dict[str, object]:
        mode = str(state.get("mode", "battery"))
        if mode not in _POWER_MODES:
            mode = "battery"
        rails: list[dict[str, str | None]] = []
        raw_rails = state.get("rails")
        if isinstance(raw_rails, list):
            for rail in raw_rails:
                if len(rails) >= 2:
                    break
                if not isinstance(rail, dict):
                    continue
                rails.append(
                    {
                        "v": str(rail["v"]) if rail.get("v") else None,
                        "i": str(rail["i"]) if rail.get("i") else None,
                    }
                )
        return {"mode": mode, "rails": rails}

    @staticmethod
    def _decode_power(raw: str) -> dict[str, object]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"mode": "battery", "rails": []}
        if not isinstance(data, dict):
            return {"mode": "battery", "rails": []}
        return CaptureLibrary._clean_power(cast(dict[str, object], data))

    def _channel_names(self, stem: str) -> set[str]:
        csv = self.file_for(stem, "csv")
        if csv is None:
            return set()
        _, descs = self._read_metadata(csv)
        return {d["name"] for d in descs}

    def get_power(self, stem: str) -> dict[str, object]:
        names = self._channel_names(stem)
        conn = self._connect()
        try:
            with self._lock, conn:
                row = conn.execute(
                    "SELECT json FROM power_analysis WHERE stem = ?", (stem,)
                ).fetchone()
                if row is not None:
                    state = self._decode_power(row["json"])
                    return {
                        "mode": state["mode"],
                        "rails": state["rails"],
                        "source": "capture",
                    }
                for r in conn.execute(
                    "SELECT json FROM power_analysis ORDER BY saved_at DESC"
                ):
                    state = self._decode_power(r["json"])
                    rails = cast(list[dict[str, str | None]], state["rails"])
                    rail_names = {
                        n for rail in rails for n in (rail["v"], rail["i"]) if n
                    }
                    if rail_names and rail_names <= names:
                        return {
                            "mode": state["mode"],
                            "rails": rails,
                            "source": "reused",
                        }
                default_row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'power_mode'"
                ).fetchone()
                default = default_row["value"] if default_row else "battery"
                if default not in _POWER_MODES:
                    default = "battery"
                return {"mode": default, "rails": [], "source": "default"}
        finally:
            conn.close()

    def set_power(self, stem: str, state: dict[str, object]) -> dict[str, object]:
        cleaned = self._clean_power(state)
        conn = self._connect()
        try:
            with self._lock, conn:
                conn.execute(
                    "INSERT OR IGNORE INTO captures (stem, project) VALUES (?, NULL)", (stem,)
                )
                conn.execute(
                    "INSERT INTO power_analysis (stem, json, saved_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(stem) DO UPDATE SET json = excluded.json, "
                    "saved_at = excluded.saved_at",
                    (
                        stem,
                        json.dumps(cleaned),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
        finally:
            conn.close()
        return cleaned

    # -- assertions ----------------------------------------------------------

    @staticmethod
    def _clean_bound(v: object) -> float | None:
        if v is None or v == "":
            return None
        try:
            f = float(cast(Any, v))
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    @staticmethod
    def _clean_assertions(items: list[dict[str, object]]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        seen: set[str] = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "")).strip()
            if not name or name in seen:
                continue
            lo = CaptureLibrary._clean_bound(it.get("min"))
            hi = CaptureLibrary._clean_bound(it.get("max"))
            if lo is None and hi is None:
                continue
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            seen.add(name)
            out.append({"name": name, "min": lo, "max": hi})
        return out

    def get_assertions(self) -> list[dict[str, object]]:
        raw = self.get_setting("assertions")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        return self._clean_assertions(cast(list[dict[str, object]], data))

    def set_assertions(
        self, items: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        cleaned = self._clean_assertions(items)
        self.set_setting("assertions", json.dumps(cleaned))
        return cleaned

    def check_assertions(self, stem: str) -> dict[str, object]:
        assertions = self.get_assertions()
        csv = self.file_for(stem, "csv")
        series: list[dict[str, Any]] = []
        if csv is not None:
            data = self.parse_csv(csv)
            series = cast(list[dict[str, Any]], data.get("series") or [])
        by_name: dict[str, dict[str, Any]] = {}
        for item in series:
            n = str(item.get("name", ""))
            if n and n not in by_name:
                by_name[n] = item

        results: list[dict[str, object]] = []
        for a in assertions:
            name = str(a["name"])
            lo = cast(float | None, a.get("min"))
            hi = cast(float | None, a.get("max"))
            s = by_name.get(name)
            if s is None:
                results.append(
                    {
                        "name": name,
                        "min": lo,
                        "max": hi,
                        "actual_min": None,
                        "actual_max": None,
                        "unit": "",
                        "found": False,
                        "pass": False,
                    }
                )
                continue
            pts = cast(list[list[float | None]], s.get("points") or [])
            vals = [v for _, v in pts if v is not None]
            if not vals:
                results.append(
                    {
                        "name": name,
                        "min": lo,
                        "max": hi,
                        "actual_min": None,
                        "actual_max": None,
                        "unit": str(s.get("unit", "") or ""),
                        "found": False,
                        "pass": False,
                    }
                )
                continue
            amin = min(vals)
            amax = max(vals)
            ok = True
            if lo is not None and amin < lo:
                ok = False
            if hi is not None and amax > hi:
                ok = False
            results.append(
                {
                    "name": name,
                    "min": lo,
                    "max": hi,
                    "actual_min": amin,
                    "actual_max": amax,
                    "unit": str(s.get("unit", "") or ""),
                    "found": True,
                    "pass": ok,
                }
            )
        passed = sum(1 for r in results if r["pass"])
        return {"results": results, "passed": passed, "checks": len(results)}

    # -- listing / scan ------------------------------------------------------

    def scan(self) -> list[dict[str, object]]:
        files = self._iter_files()
        stems = {f.stem for f in files}

        conn = self._connect()
        try:
            with self._lock:
                self._reconcile(conn, stems)
                projects = {
                    r["name"]: r["retention_days"]
                    for r in conn.execute("SELECT name, retention_days FROM projects")
                }
                assigned = {
                    r["stem"]: r["project"]
                    for r in conn.execute("SELECT stem, project FROM captures")
                }
        finally:
            conn.close()

        global_retention = (
            load_config().get("settings", {}).get("retention_days")
        )
        now = datetime.now()

        records: list[dict[str, object]] = []
        for path in files:
            serial, tag, captured = self._parse_stem(path.stem)
            if captured is None:
                captured = datetime.fromtimestamp(path.stat().st_mtime)
            project = assigned.get(path.stem)
            retention = (
                projects.get(project) if project is not None else global_retention
            )
            records.append(
                {
                    "stem": path.stem,
                    "name": path.name,
                    "kind": path.suffix.lstrip("."),
                    "path": str(path),
                    "serial": serial,
                    "tag": tag,
                    "captured_at": captured.isoformat(timespec="seconds"),
                    "size_bytes": path.stat().st_size,
                    "project": project,
                    "retention_days": retention,
                    "expired": self._expired(captured, retention, now),
                }
            )
        records.sort(key=lambda r: str(r["captured_at"]), reverse=True)
        return records

    @staticmethod
    def _expired(captured: datetime, retention_days: int | None, now: datetime) -> bool:
        if retention_days is None:
            return False
        return (now - captured).days >= retention_days

    def retention_scan(self) -> list[str]:
        """Return stems whose capture has reached its retention date."""
        return sorted({str(r["stem"]) for r in self.scan() if r["expired"]})

    def storage_stats(self) -> dict[str, object]:
        by_project: dict[str, dict[str, int]] = {}
        total = 0
        count = 0
        for rec in self.scan():
            size = cast(int, rec["size_bytes"])
            total += size
            count += 1
            key = str(rec["project"]) if rec["project"] else "(unassigned)"
            bucket = by_project.setdefault(key, {"bytes": 0, "count": 0})
            bucket["bytes"] += size
            bucket["count"] += 1
        return {
            "total_bytes": total,
            "file_count": count,
            "per_project": [
                {"project": key, "bytes": v["bytes"], "count": v["count"]}
                for key, v in sorted(by_project.items())
            ],
        }

    # -- trash ---------------------------------------------------------------

    def trash(self, stems: list[str]) -> dict[str, object]:
        targets = [p for p in self._iter_files() if p.stem in stems]
        trashed: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        for path in targets:
            ok, detail = self._trash_one(path)
            if ok:
                trashed.append({"name": path.name})
            else:
                errors.append({"name": path.name, "detail": detail})
        # Drop metadata for stems that no longer have any file on disk.
        self.scan()
        return {"trashed": trashed, "errors": errors}

    @staticmethod
    def _trash_one(path: Path) -> tuple[bool, str]:
        if shutil.which("gio"):
            argv = ["gio", "trash", str(path)]
        elif shutil.which("trash-put"):
            argv = ["trash-put", str(path)]
        else:
            return False, "no trash tool available (install gio or trash-cli)"
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip() or "trash failed"
            return False, detail
        return True, str(path)

    # -- CSV reading ---------------------------------------------------------

    def _read_metadata(self, path: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
        """Return (scalar metadata, channel descriptors) from the ``#`` header."""
        scalars: dict[str, str] = {}
        descs: list[dict[str, str]] = []
        for line in path.read_text().splitlines():
            if not line.startswith("#"):
                continue
            key, sep, value = line[1:].lstrip().partition(": ")
            if not sep:
                continue
            if key == "computed":
                match = _COMPUTED_LINE.match(value)
                if match:
                    descs.append(
                        {
                            "name": match.group(1).strip(),
                            "unit": match.group(2).strip(),
                            "expr": match.group(3).strip(),
                            "computed": "true",
                        }
                    )
            elif re.fullmatch(r"[A-Z]\d+", key):
                match = _PHYSICAL_LINE.match(value)
                if match:
                    descs.append(
                        {
                            "key": key,
                            "name": match.group(1).strip(),
                            "unit": match.group(2).strip(),
                        }
                    )
            else:
                scalars[key] = value
        return scalars, descs

    def parse_csv(self, path: Path | str) -> dict[str, object]:
        path = Path(path)
        scalars, descs = self._read_metadata(path)

        data_lines = [
            line for line in path.read_text().splitlines()
            if line and not line.startswith("#")
        ]
        if not data_lines:
            return {
                "name": path.name,
                "meta": scalars,
                "channels": descs,
                "sample_count": 0,
                "duration_s": 0.0,
                "series": [],
            }

        reader = csv.reader(data_lines)
        header = next(reader, None)
        if header is None:
            header = []
        channel_columns = header[5:] if len(header) > 5 else []

        # Name each series by its CSV column (already deduplicated on write), so a
        # capture whose profile reused a label still reads back with unique names.
        # Units come from the metadata descriptors, positionally, since the header
        # carries no units.
        names: list[str] = []
        units: list[str] = []
        for i in range(len(channel_columns)):
            names.append(channel_columns[i])
            units.append(descs[i]["unit"] if i < len(descs) else "")

        points: list[list[list[float | None]]] = [[] for _ in channel_columns]
        sample_count = 0
        duration = 0.0
        for row in reader:
            if len(row) <= 5:
                continue
            try:
                elapsed = float(row[1])
            except (ValueError, IndexError):
                continue
            sample_count += 1
            if elapsed > duration:
                duration = elapsed
            for i in range(len(channel_columns)):
                value: float | None = None
                if 5 + i < len(row):
                    try:
                        value = float(row[5 + i])
                    except ValueError:
                        value = None
                points[i].append([elapsed, value])

        series: list[dict[str, object]] = [
            {"name": names[i], "unit": units[i], "points": points[i]}
            for i in range(len(channel_columns))
        ]

        return {
            "name": path.name,
            "meta": scalars,
            "channels": descs,
            "columns": header,
            "sample_count": sample_count,
            "duration_s": round(duration, 6),
            "series": series,
        }

    # -- config reconstruction ----------------------------------------------

    def config_from_csv(self, path: Path | str) -> dict[str, Any] | None:
        """Build a runtime config from a CSV's embedded (or reconstructed) profile."""
        path = Path(path)
        scalars, descs = self._read_metadata(path)

        embedded = scalars.get("config")
        if embedded:
            try:
                data = json.loads(embedded)
            except ValueError:
                data = {}
            name = str(data.get("active_profile") or "capture")
            profile = data.get("profile") or {}
            return {
                "active_profile": name,
                "profiles": {name: profile},
                "settings": data.get("settings", {}),
            }
        return self._reconstruct_config(scalars, descs)

    @staticmethod
    def _reconstruct_config(
        scalars: dict[str, str], descs: list[dict[str, str]]
    ) -> dict[str, Any]:
        default_channel = DEFAULT_CONFIG["profiles"]["default"]["channels"]
        channels: dict[str, Any] = {}
        for key in CHANNEL_KEYS:
            base = dict(default_channel[key])
            channels[key] = base

        seen = 0
        for desc in descs:
            if desc.get("computed"):
                continue
            key = desc["key"]
            channels[key] = {
                "name": desc["name"],
                "unit": desc["unit"],
                "gain": channels[key]["gain"],
                "offset": channels[key]["offset"],
                "show": True,
                "color": channels[key]["color"],
            }
            seen += 1
        if seen:
            for key in CHANNEL_KEYS:
                if channels[key]["name"] in (key, ""):
                    channels[key]["show"] = False

        computed = [
            {
                "name": d["name"],
                "unit": d["unit"],
                "expr": d.get("expr", ""),
                "show": True,
                "color": PALETTE[(len(CHANNEL_KEYS) + j) % len(PALETTE)],
            }
            for j, d in enumerate(descs)
            if d.get("computed")
        ]

        settings: dict[str, Any] = {}
        rate = scalars.get("sample_rate_hz")
        if rate:
            with contextlib.suppress(ValueError):
                settings["sample_rate_hz"] = float(rate)

        name = scalars.get("profile") or "capture"
        return {
            "active_profile": name,
            "profiles": {name: {"channels": channels, "computed": computed}},
            "settings": settings,
        }
