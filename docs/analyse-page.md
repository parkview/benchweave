# Analyse page

The **Analyse** tab of the ADC web frontend is where captured data goes to be
reviewed, re-plotted, organised and retired. It lists every CSV and PNG the
board has written, re-draws a graph from any CSV, groups captures into
projects, and suggests files that have passed their retention date.

## Running it

Start the web frontend from the repository root:

```sh
./scripts/run_adc_web.sh
```

Then open <http://localhost:8000> and click the **Analyse** tab. (Set
`BENCHWEAVE_PORT` / `BENCHWEAVE_HOST` to listen elsewhere; see the script for
details.)

## What you see

- **CSV files** — one frame lists every `.csv` capture, newest first, with its
  capture time and size. **Click a filename to plot it.**
- **PNG files** — a second frame lists every saved chart `.png`. Click one to
  open the image in a new tab.
- **Storage** — the total space used and a per-project breakdown.
- **Retention suggestions** — captures that have reached their retention date,
  pre-ticked and waiting for you to confirm.

A CSV and the PNG/HTML derived from it share a filename *stem*, so they are
grouped together: assigning a capture to a project, or trashing it, applies to
the whole group.

## Plotting a capture

Click a CSV filename to load it. The viewer draws every channel against elapsed
time — voltage on the left axis, current on the right.

- **Scroll** over the chart to zoom the time axis.
- **Drag** across a region to select it and read off the energy it represents:
  **Ah** (`∫ I dt`) for the current channel, **Wh** (`∫ V·I dt`) for the
  voltage channel chosen in the **Voltage channel** picker, and the **average
  and peak power** (W) of that same V×I pair.
- A **region inspector** table below the chart then lists, for every channel in
  the selection, its **min / mean / max / RMS / peak-to-peak** over the region.
- **Reset zoom** clears the zoom window, the selection, and the stats table.

## Projects and retention

1. Create a project with **New project**, giving it a name and (optionally) a
   retention period in days. Leaving retention blank means "inherit the global
   `settings.retention_days`".
2. Assign a capture to a project using the dropdown in its CSV row.
3. Change a project's retention by selecting it, entering days, and clicking
   **Set retention**.

Retention is advisory, never silent: once a capture is older than its project's
retention (or the global default if unassigned), it appears under **Retention
suggestions** with its checkbox already ticked. Review the list and click
**Move selected to trash**. Files go to the operating-system trash, so they
remain recoverable for a while — nothing is permanently deleted here.

## Reusing a capture's configuration

Each capture records the board profile it was made with. Click **Apply config
to board** to push that profile and its settings to the connected ADC board —
handy for reproducing a run. (New captures embed the profile directly; older
ones are reconstructed from the `#` header lines.)

## Where things live

- Captures: `plugins/adc_6ch_12bit/captures/` (CSV, PNG, HTML; gitignored).
- Project and retention metadata: `plugins/adc_6ch_12bit/library.db` (SQLite;
  gitignored). The filesystem stays the source of truth — the database only
  overlays project assignment and retention.
