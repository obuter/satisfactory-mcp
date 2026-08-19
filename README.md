# satisfactory-mcp

A **read-only [MCP](https://modelcontextprotocol.io) connector for Satisfactory** — it reads
the [Ficsit Remote Monitoring (FRM)](https://github.com/porisius/FicsitRemoteMonitoring) mod's
JSON API and exposes your live factory (power, production, idle machines, logistics, the
AWESOME Sink) to any MCP client, so an AI agent can watch the factory and help you build it.

Two layers that compose:

1. **The connector** (`connector/`) — a small FastMCP server giving an agent *facts* about the
   factory through 10 read-only tools.
2. **The assistant skill** (`skill/`) — a persona/directive file that gives the agent
   *judgment*: how to reason about those facts like a real Satisfactory architect (dual-grid
   power, the 480 m³/min pipe cap, 6+1 trains, underclocking, neat permanent builds). Edit it
   to match your own play style.

Optionally, `monitoring/` ships curated Grafana + Prometheus dashboards for the same data.

> **Read-only by design.** The FRM mod has write endpoints (flip switches, set priorities);
> this connector deliberately exposes none. The agent observes and advises — you build.

---

## Requirements

- **Satisfactory** with the **Ficsit Remote Monitoring** mod installed and running. It serves
  JSON over plain HTTP (default port `8080`) *only while a save is loaded*.
- **Docker** (to run the connector), or Python 3.12+ to run it directly.
- An **MCP client**: Claude Desktop, Claude Code, Google Antigravity, or anything that speaks
  MCP over SSE/streamable-HTTP.

## Quickstart (connector)

```bash
cd connector
cp ../.env.example .env
# edit .env: set FRM_HOST to the machine running Satisfactory + the FRM mod
docker compose up -d
```

The connector is now live at `http://localhost:8029` (`/sse` and `/mcp`). Check it:

```bash
curl -s http://localhost:8029/sse | head    # should open an event stream
```

### Wire it into your MCP client

- **Claude Code** — copy `.mcp.json.example` to `.mcp.json` in your workspace (points at
  `http://localhost:8029/sse`).
- **Claude Desktop** — add to `claude_desktop_config.json`, using the `mcp-remote` bridge:
  ```json
  "satisfactory": {
    "command": "npx",
    "args": ["-y", "mcp-remote", "http://localhost:8029/sse", "--allow-http"]
  }
  ```
- **Google Antigravity** — register `http://localhost:8029/sse` in its MCP settings, and open
  the `skill/` folder (or copy its `CLAUDE.md`/`.geminirules`) as workspace context.

## Tools

| Tool | What it does |
|------|--------------|
| `health_check` | Confirm the mod is reachable / a save is loaded. |
| `factory_summary` | Building counts by type, top clusters, overall shape. |
| `power_status` | Per-circuit generation vs consumption, fuse trips, batteries. |
| `production_stats` | Item production/consumption rates, deficits, stalls. |
| `list_buildings` | Enumerate/filter machines by status (`idle`/`paused`/`unconfigured`/`problem`/`all`), type, or recipe — paginated. |
| `find_bottlenecks` | Cross-references production deficits with idle producers to name choke points. |
| `logistics_status` | Trains, trucks, drones, vehicle stations. |
| `inventory_search` | Where an item is stored / how much exists. |
| `sink_status` | AWESOME Sink points, coupons, current item. |
| `get_endpoint` | Raw FRM endpoint escape hatch, with field projection + paging. |

## The assistant skill

`skill/CLAUDE.md` is the persona — it tells the agent *how you like to build*. `.geminirules`
and `AGENTS.md` are symlinks to it (one source of truth), and
`.claude/skills/satisfactory-assistant/SKILL.md` makes it an auto-triggering skill in Claude
Code. **Edit `CLAUDE.md` to describe your own factory philosophy** — the shipped version is one
player's opinionated style, not gospel.

## Optional: Grafana dashboards

```bash
cd monitoring
cp ../.env.example .env   # set FRM_HOST, and a real GF_SECURITY_ADMIN_PASSWORD
docker compose up -d
```

Grafana → `http://localhost:3031`, Prometheus → `http://localhost:9090`. Five provisioned
dashboards: overview, power, production, logistics, sink/inventory.

## Configuration

All via environment variables (see `.env.example`):

| Var | Default | Meaning |
|-----|---------|---------|
| `FRM_HOST` | `127.0.0.1` | Address of the machine running Satisfactory + FRM mod. |
| `FRM_PORT` | `8080` | FRM mod HTTP port. |
| `FRM_TIMEOUT` | `10` | Per-request timeout (seconds). |
| `MAX_OUTPUT_CHARS` | `8000` | Cap on a tool's raw output before it truncates. |

## Credits

- [Ficsit Remote Monitoring](https://github.com/porisius/FicsitRemoteMonitoring) by **porisius**
  — the in-game mod this connector reads. This project is an independent client, not affiliated.
- [FicsitRemoteMonitoringCompanion](https://github.com/AP-Hunt/FicsitRemoteMonitoringCompanion)
  by **AP-Hunt** — the Prometheus exporter used by the optional monitoring stack.
- Satisfactory © Coffee Stain Studios. This is an unofficial fan tool.

## License

[MIT](LICENSE).
