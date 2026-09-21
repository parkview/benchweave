# Hardware compatibility

Hardware tested with BenchWeave and confirmed working, together with hardware
awaiting testing. Add a row as each device is validated.

| Hardware | Status | Connection / firmware | Notes |
|---|---|---|---|
| nanoDLA **v1.3** | Tested — working | USB `1d50:608c` · FX2LP + `fx2lafw` | End-to-end UART loopback capture + decode validated 2026-09-18 via the [nanoDLA MCP server](nanodla-mcp-server.md) |
| nanoDLA **v2.1** | Awaiting testing | — unverified | Not yet tested; whether it still enumerates as `fx2lafw` is unconfirmed |

## Scope

The [nanoDLA MCP server](nanodla-mcp-server.md) targets the `fx2lafw` sigrok
driver. Any FX2-based logic analyser that enumerates as `fx2lafw` is a candidate
for this table but needs its own test row before it is listed as working. sigrok
supports many further devices; making those available through the server would
require selecting the driver per device rather than hard-coding `fx2lafw`.
