#!/usr/bin/env python3
"""
satisfactory — read-only MCP connector for the Ficsit Remote Monitoring (FRM)
mod's JSON web API, so an agent can watch a live Satisfactory factory.

Backend: the FRM mod serves JSON over plain HTTP on the *game* machine
(FRM_HOST:FRM_PORT, default 127.0.0.1:8080 — set FRM_HOST to your game PC's
address). It only answers while the game is running with a save loaded — a
connection error therefore means "game offline", not a connector fault.

Design contract:
  - 4–8 verb-tools, one tool = one complete job.
  - READ-ONLY: the FRM mod has write endpoints (switches, priorities) but this
    connector deliberately exposes none — every tool is readOnlyHint=True.
  - Every tool returns a STRING; errors are "[ERROR] ..." strings, never raised.
  - Payloads are huge (/getFactory alone is megabytes) — tools SUMMARISE, they
    never dump. get_endpoint is the allowlisted raw escape hatch.

Transport: /mcp (streamable HTTP) + /sse (legacy). Endpoint: http://<host>:8029/mcp
"""

import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

# ── Logging (stderr — stdout is reserved for the transport) ─────────────────
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# ── Config from env ─────────────────────────────────────────────────────────
CONNECTOR_NAME = os.environ.get("CONNECTOR_NAME", "satisfactory")
PORT           = int(os.environ.get("PORT", "8029"))
FRM_HOST       = os.environ.get("FRM_HOST", "127.0.0.1")
FRM_PORT       = int(os.environ.get("FRM_PORT", "8080"))
FRM_TIMEOUT    = float(os.environ.get("FRM_TIMEOUT", "10"))
MAX_OUTPUT     = int(os.environ.get("MAX_OUTPUT_CHARS", "8000"))

BASE_URL = f"http://{FRM_HOST}:{FRM_PORT}"

logger = logging.getLogger(f"mcp-{CONNECTOR_NAME}")

# Full read-endpoint registry of the FRM mod (extracted from
# porisius/FicsitRemoteMonitoring source). get_endpoint may fetch any of these;
# nothing outside this set is reachable, and no write endpoints are listed.
FRM_READ_ENDPOINTS = {
    "getAll", "getArtifacts", "getAssembler", "getBelts", "getBiomassGenerator",
    "getBlender", "getBlueprints", "getCables", "getChatMessages", "getCloudInv",
    "getCoalGenerator", "getConstructor", "getConverter", "getCrateInv",
    "getCreatures", "getDoggo", "getDrone", "getDroneStation", "getDropPod",
    "getElevators", "getEncoder", "getExplorationSink", "getExplorer",
    "getExtractor", "getFactory", "getFactoryCart", "getFallingGiftBundles",
    "getFoundry", "getFrackingActivator", "getFuelGenerator", "getGenerators",
    "getGeothermalGenerator", "getHUBTerminal", "getHazards", "getHyperEntrance",
    "getHyperJunctions", "getHypertube", "getItemPickups", "getLifts",
    "getManufacturer", "getMapMarkers", "getModList", "getNuclearGenerator",
    "getPackager", "getParticle", "getPipeJunctions", "getPipes", "getPlayer",
    "getPortal", "getPower", "getPowerSlug", "getPowerUsage", "getProdStats",
    "getPump", "getRadarTower", "getRecipes", "getRefinery", "getResearchTrees",
    "getResourceDeposit", "getResourceGeyser", "getResourceNode",
    "getResourceSink", "getResourceSinkBuilding", "getResourceWell", "getSPWN",
    "getSchematics", "getSessionInfo", "getSinkList", "getSmelter",
    "getSpaceElevator", "getSpawners", "getSplitterMerger", "getSporeFlowers",
    "getStorageInv", "getSwitches", "getTapes", "getThroughputCounter",
    "getTractor", "getTradingPost", "getTrainRails", "getTrainSignals",
    "getTrainStation", "getTrains", "getTruck", "getTruckStation",
    "getUObjectCount", "getUnlockItems", "getVehiclePaths", "getVehicles",
    "getWorldInv",
}

_SERVER_START = time.time()

mcp = FastMCP(CONNECTOR_NAME, host="0.0.0.0", port=PORT)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _audit(tool: str, args_repr: str) -> None:
    """One structured audit line per call. Args are hashed, never logged raw."""
    sys.stderr.write(json.dumps({
        "event": "tool_call",
        "connector": CONNECTOR_NAME,
        "tool": tool,
        "args_hash": hashlib.sha256(args_repr.encode()).hexdigest()[:12],
        "ts": time.time(),
    }) + "\n")


def _truncate(text: str) -> str:
    return text[:MAX_OUTPUT] + ("…[truncated]" if len(text) > MAX_OUTPUT else "")


async def _frm_get(endpoint: str) -> tuple[Any, str | None]:
    """
    GET one FRM endpoint and parse JSON. Returns (data, None) on success or
    (None, "[ERROR] ...") on failure. A connection error is reported as the game
    being offline — the FRM server only answers while a save is loaded.
    """
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=FRM_TIMEOUT) as client:
            resp = await client.get(url)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return None, ("[ERROR] FRM unreachable at "
                      f"{BASE_URL} — is Satisfactory running with a save loaded?")
    except httpx.HTTPError as e:
        return None, f"[ERROR] request to /{endpoint} failed: {e}"
    if resp.status_code != 200:
        return None, f"[ERROR] /{endpoint} returned HTTP {resp.status_code}"
    try:
        return resp.json(), None
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"[ERROR] /{endpoint} returned non-JSON: {e}"


def _num(v: Any) -> float:
    """Best-effort float; FRM sends floats but guard against odd values."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt(v: float) -> str:
    """Compact number: no trailing .0, thousands separators."""
    if abs(v - round(v)) < 0.05:
        return f"{int(round(v)):,}"
    return f"{v:,.1f}"


# ── /getFactory building helpers (shared by factory_summary, list_buildings,
#    find_bottlenecks). /getFactory is multi-MB; these condense one building at
#    a time so tools never return the raw fat objects. ────────────────────────
def _building_type(b: dict) -> str:
    return str(b.get("Name") or b.get("ClassName") or "?")


def _building_status(b: dict) -> str:
    """Classify a production building: unconfigured | paused | producing | idle."""
    if b.get("IsConfigured") is False:
        return "unconfigured"
    if b.get("IsPaused"):
        return "paused"
    if b.get("IsProducing"):
        return "producing"
    return "idle"


def _idle_reason(b: dict) -> str:
    """
    Heuristic for *why* an idle building isn't producing, from the inventory the
    API already returns:
      - fuse tripped        → unpowered
      - an input at 0       → input-starved (upstream bottleneck)
      - an output at max    → output-blocked (downstream belt full)
    """
    pi = b.get("PowerInfo") or {}
    if pi.get("FuseTriggered"):
        return "unpowered (fuse tripped)"
    inputs = b.get("InputInventory") or []
    starved = [str(i.get("Name")) for i in inputs if _num(i.get("Amount")) == 0]
    if starved:
        return "input-starved (empty): " + ", ".join(dict.fromkeys(starved))
    blocked = [str(o.get("Name")) for o in (b.get("OutputInventory") or [])
               if _num(o.get("MaxAmount")) > 0
               and _num(o.get("Amount")) >= _num(o.get("MaxAmount"))]
    if blocked:
        return "output-blocked: " + ", ".join(dict.fromkeys(blocked))
    # Nothing conclusive — surface the actual input buffers so a trickle-starved
    # machine (low but non-zero) or a fluid/clock issue is judgeable by hand.
    if inputs:
        levels = ", ".join(f"{i.get('Name')} {_fmt(_num(i.get('Amount')))}/"
                           f"{_fmt(_num(i.get('MaxAmount')))}" for i in inputs[:4])
        return f"idle; inputs {levels}"
    return "idle (no solid inputs — fluid line or clock/power)"


def _loc(b: dict) -> str:
    """Compact world coordinates — paste into an in-game map ping."""
    l = b.get("location") or {}
    return f"({_num(l.get('x')):.0f}, {_num(l.get('y')):.0f}, {_num(l.get('z')):.0f})"


# ════════════════════════════════════════════════════════════════════════════
#  power_status — the "am I about to brown out?" tool
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Power Status",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def power_status() -> str:
    """
    Per power-circuit production, consumption, capacity and headroom from
    /getPower. Flags tripped fuses and circuits whose peak draw exceeds capacity
    (a brownout waiting to happen). Empty/unpowered circuit groups are omitted.
    """
    _audit("power_status", "")
    data, err = await _frm_get("getPower")
    if err:
        return err
    if not isinstance(data, list):
        return "[ERROR] unexpected /getPower shape"

    active = [c for c in data
              if _num(c.get("PowerCapacity")) > 0 or _num(c.get("PowerConsumed")) > 0]
    if not active:
        return "No powered circuits found (factory idle or unpowered)."

    # Sort worst-headroom first so the risky circuits lead.
    def headroom(c: dict) -> float:
        cap = _num(c.get("PowerCapacity"))
        return (cap - _num(c.get("PowerConsumed"))) / cap if cap else 0.0

    active.sort(key=headroom)
    lines, warnings = [], []
    for c in active:
        gid  = c.get("CircuitGroupID")
        prod = _num(c.get("PowerProduction"))
        cons = _num(c.get("PowerConsumed"))
        cap  = _num(c.get("PowerCapacity"))
        peak = _num(c.get("PowerMaxConsumed"))
        batt = _num(c.get("BatteryPercent"))
        hr   = (cap - cons) / cap * 100 if cap else 0.0
        fuse = bool(c.get("FuseTriggered"))
        flag = ""
        if fuse:
            flag = "  ⚠ FUSE TRIPPED"
            warnings.append(f"circuit group {gid}: fuse tripped")
        elif peak > cap > 0:
            flag = "  ⚠ peak draw > capacity"
            warnings.append(f"circuit group {gid}: peak {_fmt(peak)} > cap {_fmt(cap)} MW")
        elif cap and hr < 5:
            flag = "  ⚠ <5% headroom"
            warnings.append(f"circuit group {gid}: only {hr:.0f}% headroom")
        batt_str = f", batt {batt:.0f}%" if _num(c.get("BatteryCapacity")) > 0 else ""
        lines.append(
            f"• group {gid}: {_fmt(cons)}/{_fmt(cap)} MW used "
            f"({hr:.0f}% free), peak {_fmt(peak)} MW{batt_str}{flag}"
        )

    header = f"{len(active)} active circuit group(s)"
    if warnings:
        header += f" — {len(warnings)} WARNING(S)"
    return _truncate(header + ":\n" + "\n".join(lines))


# ════════════════════════════════════════════════════════════════════════════
#  production_stats — item production vs consumption balance
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Production Stats",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def production_stats(
    item_filter: Annotated[str, Field(
        description="Case-insensitive substring to match item names, e.g. 'iron'. Empty = all items.")] = "",
    problems_only: Annotated[bool, Field(
        description="If true, only show items running a deficit (consumed > produced) or stalled.")] = True,
) -> str:
    """
    Per-item production vs consumption from /getProdStats. Surfaces DEFICITS
    (consuming faster than producing → stores will drain) and STALLED items
    (producers exist but making nothing). Use item_filter to inspect one product.
    """
    _audit("production_stats", f"{item_filter}|{problems_only}")
    data, err = await _frm_get("getProdStats")
    if err:
        return err
    if not isinstance(data, list):
        return "[ERROR] unexpected /getProdStats shape"

    flt = item_filter.strip().lower()
    rows = []
    for it in data:
        name = str(it.get("Name", "?"))
        if flt and flt not in name.lower():
            continue
        prod = _num(it.get("CurrentProd"))
        cons = _num(it.get("CurrentConsumed"))
        eff  = _num(it.get("ProdPercent"))       # producer utilisation %
        net  = prod - cons
        stalled = prod == 0 and cons > 0
        deficit = net < -0.5
        rows.append({
            "name": name, "prod": prod, "cons": cons, "net": net,
            "eff": eff, "stalled": stalled, "deficit": deficit,
        })

    if not rows:
        return (f"No production items match '{item_filter}'."
                if flt else "No production data (factory idle?).")

    shown = [r for r in rows if r["deficit"] or r["stalled"]] if problems_only else rows
    if problems_only and not shown:
        return (f"{len(rows)} item(s) tracked — none in deficit. "
                "Production is keeping up with consumption. ✓")

    shown.sort(key=lambda r: r["net"])   # most negative (worst) first
    lines = []
    for r in shown[:60]:
        tag = "  ⚠ STALLED" if r["stalled"] else ("  ⚠ deficit" if r["deficit"] else "")
        lines.append(
            f"• {r['name']}: prod {_fmt(r['prod'])}/min, cons {_fmt(r['cons'])}/min, "
            f"net {'+' if r['net'] >= 0 else ''}{_fmt(r['net'])}/min "
            f"(eff {r['eff']:.0f}%){tag}"
        )
    header = (f"{len(shown)} problem item(s) of {len(rows)} tracked"
              if problems_only else f"{len(shown)} item(s)")
    return _truncate(header + ":\n" + "\n".join(lines))


# ════════════════════════════════════════════════════════════════════════════
#  factory_summary — condense the (huge) building list
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Factory Summary",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def factory_summary() -> str:
    """
    Aggregate view of all production buildings from /getFactory: counts by
    machine type, how many are idle/paused/unconfigured, and total power draw.
    Condenses a multi-megabyte payload — never returns the raw building list.
    """
    _audit("factory_summary", "")
    data, err = await _frm_get("getFactory")
    if err:
        return err
    if not isinstance(data, list):
        return "[ERROR] unexpected /getFactory shape"
    if not data:
        return "No production buildings reported."

    by_type: Counter = Counter()
    idle = paused = unconfigured = 0
    total_power = 0.0
    clusters: Counter = Counter()   # problem buildings grouped by "type · recipe"
    for b in data:
        by_type[_building_type(b)] += 1
        st = _building_status(b)
        if st == "paused":
            paused += 1
        elif st == "unconfigured":
            unconfigured += 1
            clusters[f"{_building_type(b)} · unconfigured"] += 1
        elif st == "idle":
            idle += 1
            clusters[f"{_building_type(b)} · {b.get('Recipe') or 'no recipe'}"] += 1
        total_power += _num((b.get("PowerInfo") or {}).get("PowerConsumed"))

    top = by_type.most_common(15)
    lines = [f"{len(data)} production buildings, {_fmt(total_power)} MW total draw"]
    problems = []
    if idle:
        problems.append(f"{idle} idle (not producing)")
    if paused:
        problems.append(f"{paused} paused")
    if unconfigured:
        problems.append(f"{unconfigured} unconfigured")
    lines.append("Problems: " + (", ".join(problems) if problems else "none ✓"))
    if clusters:
        lines.append("Top idle/unconfigured clusters (type · recipe):")
        lines += [f"  • {key}: {n}" for key, n in clusters.most_common(12)]
        lines.append("  → list_buildings(status='problem', recipe_filter=…) for exact locations")
    lines.append("By type:")
    lines += [f"  • {t}: {n}" for t, n in top]
    if len(by_type) > len(top):
        lines.append(f"  … +{len(by_type) - len(top)} more types")
    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════════════════════
#  list_buildings — filtered, paginated, located list of buildings
# ════════════════════════════════════════════════════════════════════════════
_STATUS_SETS: dict[str, set[str]] = {
    "idle":         {"idle"},
    "paused":       {"paused"},
    "unconfigured": {"unconfigured"},
    "producing":    {"producing"},
    "problem":      {"idle", "unconfigured"},
    "all":          {"idle", "paused", "unconfigured", "producing"},
}


@mcp.tool(annotations={
    "title": "List Buildings",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def list_buildings(
    status: Annotated[str, Field(
        description="Which buildings: idle | unconfigured | paused | producing | problem "
                    "(idle+unconfigured) | all.")] = "problem",
    type_filter: Annotated[str, Field(
        description="Case-insensitive machine-type substring, e.g. 'constructor'. Empty = any.")] = "",
    recipe_filter: Annotated[str, Field(
        description="Case-insensitive recipe substring, e.g. 'screw'. Empty = any.")] = "",
    offset: Annotated[int, Field(description="Skip this many matches (paging).", ge=0)] = 0,
    limit: Annotated[int, Field(description="Max buildings to return (default 50).", ge=1, le=200)] = 50,
) -> str:
    """
    Enumerate individual production buildings from /getFactory with a compact
    projection (type, recipe, world location, and — for idle ones — a likely
    reason). Filter by status/type/recipe and PAGE with offset/limit to walk the
    full set. This is how you find *which* buildings are idle/unconfigured and
    where they are; factory_summary only gives counts.
    """
    _audit("list_buildings", f"{status}|{type_filter}|{recipe_filter}|{offset}|{limit}")
    want = _STATUS_SETS.get(status.strip().lower())
    if want is None:
        return "[ERROR] status must be one of: " + ", ".join(_STATUS_SETS)
    data, err = await _frm_get("getFactory")
    if err:
        return err
    if not isinstance(data, list):
        return "[ERROR] unexpected /getFactory shape"

    tf, rf = type_filter.strip().lower(), recipe_filter.strip().lower()
    matched = []
    for b in data:
        st = _building_status(b)
        if st not in want:
            continue
        typ = _building_type(b)
        rec = str(b.get("Recipe") or "")
        if tf and tf not in typ.lower() and tf not in str(b.get("ClassName", "")).lower():
            continue
        if rf and rf not in rec.lower():
            continue
        matched.append((b, st, typ, rec))

    total = len(matched)
    if total == 0:
        return f"No buildings match status={status}, type~'{type_filter}', recipe~'{recipe_filter}'."

    page = matched[offset:offset + limit]
    lines = []
    for b, st, typ, rec in page:
        rec_s = f" [{rec}]" if rec else " [no recipe]"
        reason = _idle_reason(b) if st == "idle" else st
        lines.append(f"• {typ}{rec_s} @ {_loc(b)} — {reason}")
    end = offset + len(page)
    footer = f"showing {offset + 1}–{end} of {total}"
    if end < total:
        footer += f"  (more: offset={end})"
    return _truncate(f"{total} building(s) [status={status}]:\n" + "\n".join(lines) + f"\n[{footer}]")


# ════════════════════════════════════════════════════════════════════════════
#  find_bottlenecks — cross-reference deficits with idle producers
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Find Bottlenecks",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def find_bottlenecks(
    max_items: Annotated[int, Field(description="Max problem items to report (default 12).", ge=1, le=40)] = 12,
) -> str:
    """
    The "what do I fix?" tool. Cross-references production deficits/stalls
    (/getProdStats) with idle producers (/getFactory) so a shortage is reported
    together with the exact idle machines that should be making it — and the
    inputs those idle machines are starved on (the root cause upstream).
    """
    _audit("find_bottlenecks", str(max_items))
    prod, err = await _frm_get("getProdStats")
    if err:
        return err
    fac, err2 = await _frm_get("getFactory")
    if err2:
        return err2
    if not isinstance(prod, list) or not isinstance(fac, list):
        return "[ERROR] unexpected /getProdStats or /getFactory shape"

    problems = []
    for it in prod:
        p, c = _num(it.get("CurrentProd")), _num(it.get("CurrentConsumed"))
        if p == 0 and c > 0:
            problems.append((str(it.get("Name")), "STALLED", c - p))
        elif c - p > 0.5:
            problems.append((str(it.get("Name")), "deficit", c - p))
    problems.sort(key=lambda x: -x[2])

    # Idle producers keyed by the item they should output; and a tally of the
    # inputs idle machines are starved on (the likely upstream root cause).
    idle_by_item: dict[str, list] = {}
    starved: Counter = Counter()
    for b in fac:
        if _building_status(b) != "idle":
            continue
        for pr in (b.get("production") or []):
            idle_by_item.setdefault(str(pr.get("Name")), []).append(b)
        for i in (b.get("InputInventory") or []):
            if _num(i.get("Amount")) == 0:
                starved[str(i.get("Name"))] += 1

    if not problems and not starved:
        return "No production deficits and no idle producers — factory is balanced. ✓"

    lines = []
    if problems:
        lines.append(f"Shortages ({len(problems)}), worst first:")
        for name, kind, gap in problems[:max_items]:
            idlers = idle_by_item.get(name, [])
            note = ""
            if idlers:
                locs = ", ".join(_loc(b) for b in idlers[:3])
                more = f" +{len(idlers) - 3}" if len(idlers) > 3 else ""
                note = f" — {len(idlers)} idle producer(s){more} at {locs}"
            lines.append(f"• {name}: {kind}, short {_fmt(gap)}/min{note}")
    if starved:
        lines.append("Idle machines are most starved on (fix these upstream first):")
        lines += [f"  • {item}: starving {n} machine(s)" for item, n in starved.most_common(8)]
    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════════════════════
#  logistics_status — trains, drones, vehicles
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Logistics Status",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def logistics_status() -> str:
    """
    Fleet health from /getTrains, /getDrone and /getVehicles: derailed trains,
    train load factors, and vehicles that are out of fuel. Flags problems first.
    """
    _audit("logistics_status", "")
    out, warnings = [], []

    trains, err = await _frm_get("getTrains")
    if err:
        return err
    if isinstance(trains, list) and trains:
        derailed = [t for t in trains if t.get("Derailed") or t.get("PendingDerail")]
        out.append(f"Trains: {len(trains)} total, {len(derailed)} derailed/at-risk")
        for t in trains[:12]:
            load = (_num(t.get("PayloadMass")) / _num(t.get("MaxPayloadMass")) * 100
                    if _num(t.get("MaxPayloadMass")) else 0.0)
            d = "  ⚠ DERAILED" if (t.get("Derailed") or t.get("PendingDerail")) else ""
            out.append(f"  • {t.get('Status', '?')} @ {t.get('TrainStation', '?')}, "
                       f"load {load:.0f}%{d}")
        if derailed:
            warnings.append(f"{len(derailed)} train(s) derailed/at-risk")
    else:
        out.append("Trains: none")

    drones, _e = await _frm_get("getDrone")
    if isinstance(drones, list) and drones:
        out.append(f"Drones: {len(drones)}")
    else:
        out.append("Drones: none")

    vehicles, _e = await _frm_get("getVehicles")
    if isinstance(vehicles, list) and vehicles:
        no_fuel = [v for v in vehicles if v.get("HasFuel") is False]
        by_type: dict[str, int] = {}
        for v in vehicles:
            k = str(v.get("Name", "?"))
            by_type[k] = by_type.get(k, 0) + 1
        out.append(f"Vehicles: {len(vehicles)} ("
                   + ", ".join(f"{n}×{k}" for k, n in sorted(by_type.items())) + "), "
                   f"{len(no_fuel)} out of fuel")
        if no_fuel:
            warnings.append(f"{len(no_fuel)} vehicle(s) out of fuel")
            for v in no_fuel[:8]:
                out.append(f"  • {v.get('Name', '?')} {v.get('ID', '')}: OUT OF FUEL ⚠")
    else:
        out.append("Vehicles: none")

    header = "Logistics OK ✓" if not warnings else "Logistics WARNINGS: " + "; ".join(warnings)
    return _truncate(header + "\n" + "\n".join(out))


# ════════════════════════════════════════════════════════════════════════════
#  inventory_search — find an item across world inventory
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Inventory Search",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def inventory_search(
    item: Annotated[str, Field(
        description="Case-insensitive substring of the item name, e.g. 'plate'. Empty = whole world inventory.")] = "",
) -> str:
    """
    Search the aggregate world inventory (/getWorldInv) for an item and return
    matching stock levels (amount held vs per-stack max). Empty item = full list.
    """
    _audit("inventory_search", item)
    data, err = await _frm_get("getWorldInv")
    if err:
        return err
    if not isinstance(data, list):
        return "[ERROR] unexpected /getWorldInv shape"

    flt = item.strip().lower()
    rows = [i for i in data if not flt or flt in str(i.get("Name", "")).lower()]
    if not rows:
        return f"No world-inventory item matches '{item}'."
    rows.sort(key=lambda i: -_num(i.get("Amount")))
    lines = [f"• {i.get('Name', '?')}: {_fmt(_num(i.get('Amount')))}"
             for i in rows[:60]]
    more = f"  … +{len(rows) - 60} more" if len(rows) > 60 else ""
    return _truncate(f"{len(rows)} match(es):\n" + "\n".join(lines) + (f"\n{more}" if more else ""))


# ════════════════════════════════════════════════════════════════════════════
#  sink_status — AWESOME Sink points & coupons
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Sink Status",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def sink_status() -> str:
    """
    AWESOME Sink progress from /getResourceSink: total points banked, coupons
    earned, and progress toward the next coupon.
    """
    _audit("sink_status", "")
    data, err = await _frm_get("getResourceSink")
    if err:
        return err
    if not isinstance(data, list) or not data:
        return "No resource sink data (no AWESOME Sink built?)."
    s = data[0]
    pct = _num(s.get("Percent")) * 100
    return _truncate(
        f"AWESOME Sink: {_fmt(_num(s.get('TotalPoints')))} points banked, "
        f"{_fmt(_num(s.get('NumCoupon')))} coupons earned.\n"
        f"Next coupon: {pct:.0f}% there, "
        f"{_fmt(_num(s.get('PointsToCoupon')))} points to go."
    )


# ════════════════════════════════════════════════════════════════════════════
#  get_endpoint — allowlisted raw escape hatch (read-only)
# ════════════════════════════════════════════════════════════════════════════
@mcp.tool(annotations={
    "title": "Get FRM Endpoint (raw)",
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
})
async def get_endpoint(
    endpoint: Annotated[str, Field(
        description="An FRM read endpoint name, e.g. 'getExtractor' or 'getSchematics'. "
                    "Must be in the allowlist (no write endpoints exist here).")],
    fields: Annotated[str, Field(
        description="Comma-separated keys to keep from each list item, e.g. "
                    "'Name,ProdPercent,location'. Empty = full objects. Shrinks big arrays.")] = "",
    offset: Annotated[int, Field(description="Skip this many list items (paging).", ge=0)] = 0,
    limit: Annotated[int, Field(description="Max list items to return; 0 = all (then truncated).", ge=0, le=500)] = 0,
) -> str:
    """
    Fetch one allowlisted FRM read endpoint and return its JSON. Fallback for the
    ~90 endpoints not wrapped by a dedicated tool. For big arrays (getFactory,
    getExtractor, …) pass `fields` to project each item down to what you need and
    `offset`/`limit` to page — the header always reports the true total count so
    you can walk the whole set instead of hitting a silent truncation.
    """
    name = endpoint.strip().lstrip("/")
    _audit("get_endpoint", f"{name}|{fields}|{offset}|{limit}")
    if name not in FRM_READ_ENDPOINTS:
        sample = ", ".join(sorted(FRM_READ_ENDPOINTS)[:12])
        return (f"[BLOCKED] '{name}' is not an allowlisted FRM read endpoint. "
                f"{len(FRM_READ_ENDPOINTS)} allowed, e.g.: {sample}, …")
    data, err = await _frm_get(name)
    if err:
        return err

    if not isinstance(data, list):
        return _truncate(json.dumps(data, indent=2))

    total = len(data)
    keep = [f.strip() for f in fields.split(",") if f.strip()]
    items: Any = data
    if keep:
        items = [{k: x.get(k) for k in keep} if isinstance(x, dict) else x for x in items]
    if limit:
        items = items[offset:offset + limit]
    header = f"// {name}: {total} item(s)"
    if limit:
        header += f", showing {offset + 1}–{min(offset + limit, total)}"
        if offset + limit < total:
            header += f" (more: offset={offset + limit})"
    if keep:
        header += f", fields={','.join(keep)}"
    return _truncate(header + "\n" + json.dumps(items, indent=2))


# ── health_check — required on every connector ──────────────────────────────
@mcp.tool(annotations={"readOnlyHint": True})
async def health_check() -> str:
    """Connector status + FRM reachability (probes /getPower, best-effort session name)."""
    _audit("health_check", "")
    uptime = int(time.time() - _SERVER_START)
    base = (f"STATUS: OPERATIONAL | connector={CONNECTOR_NAME} | port={PORT} | "
            f"uptime={uptime}s | frm={BASE_URL}")
    data, err = await _frm_get("getPower")
    if err:
        return f"{base} | game=OFFLINE ({err})"
    circuits = len(data) if isinstance(data, list) else 0
    session, _e = await _frm_get("getSessionInfo")
    sname = ""
    if isinstance(session, list) and session:
        sname = f" | session={session[0].get('SessionName', '?')}"
    elif isinstance(session, dict):
        sname = f" | session={session.get('SessionName', '?')}"
    return f"{base} | game=ONLINE | circuits={circuits}{sname}"


if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware

    logger.info("Starting mcp-%s | port=%s | frm=%s", CONNECTOR_NAME, PORT, BASE_URL)
    sse = mcp.sse_app()
    streamable = mcp.streamable_http_app()
    app = Starlette(
        routes=[*sse.routes, *streamable.routes],
        lifespan=lambda app: mcp.session_manager.run(),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)
