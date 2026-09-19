# BenchWeave — project instructions

## nanoDLA logic analyser: capture → decode → annotate

When working with the nanoDLA (`benchweave-nanodla` MCP server), every capture
must be followed by analysis recorded in the capture's manifest:

1. `capture` — writes `{stem}.vcd` (for the AI), `{stem}.sr` (for a human, open
   in PulseView) and a `{stem}.json` manifest.
2. `decode` with the appropriate decoder — its output is auto-appended to the
   manifest's `decodes` list.
3. `annotate` a short prose note on what you found — **always**, even if the line
   is idle or the decode is empty. It lands in the manifest's `notes` list.

The manifest is the human-facing record of the capture event: a human reads the
`decodes` + `notes` (and opens the `.sr` in PulseView) instead of re-running
anything. Never leave a capture without a note.

Details and gotchas (trigger/start-bit, device busy, baud guessing):
`docs/nanodla-mcp-server.md`.
