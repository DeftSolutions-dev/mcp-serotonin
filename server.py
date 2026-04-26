from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s serotonin-mcp %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("serotonin-mcp")

HTTP_HOST          = "127.0.0.1"
HTTP_PORT          = 8765
POLL_HOLD_SECONDS  = 9.0
DEFAULT_TIMEOUT    = 10.0

BLACKLIST_PATH = Path(__file__).with_name("crash_blacklist.json")

DEFAULT_BLACKLIST: dict[str, Any] = {
    "version": 1,
    "paths": [
        "game.DataModel", "game.PlaceID", "game.GetFFlag", "game.SetFFlag",
    ],
    "dive_depth_limits": [
        {"prefix": "game.Workspace.Live.", "max_depth": 1,
         "reason": "Motor6D/Humanoid chain crashes deep dive"},
    ],
    "eval_code_blocked": [
        r"game\s*\.\s*DataModel",
        r"game\s*\.\s*PlaceID",
        r"game\s*\.\s*GetFFlag",
        r"game\s*\.\s*SetFFlag",
        r":GetFFlag\s*\(",
        r":SetFFlag\s*\(",
    ],
    "history": [],
}

SAFE_MODE: bool = True
BL:        dict[str, Any] = {}

def load_blacklist() -> dict[str, Any]:
    if not BLACKLIST_PATH.exists():
        BLACKLIST_PATH.write_text(json.dumps(DEFAULT_BLACKLIST, indent=4))
        return dict(DEFAULT_BLACKLIST)
    try:
        data = json.loads(BLACKLIST_PATH.read_text(encoding="utf-8"))

        for k, v in DEFAULT_BLACKLIST.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log.warning("blacklist load failed (%s), using defaults", e)
        return dict(DEFAULT_BLACKLIST)

def save_blacklist() -> None:
    try:
        BLACKLIST_PATH.write_text(json.dumps(BL, indent=4, ensure_ascii=False))
    except Exception as e:
        log.warning("blacklist save failed: %s", e)

def check_request_safe(op: str, args: dict) -> tuple[bool, str | None]:
    args = args or {}

    if op in ("inspect", "safe_inspect"):
        target = str(args.get("target", ""))
        for p in BL.get("paths", []):
            if target == p or target.startswith(p + "."):
                return False, f"blocked path '{p}' (target={target})"

    if op == "dive":
        root      = str(args.get("root", "game.Workspace"))
        max_depth = int(args.get("max_depth", 2))
        for rule in BL.get("dive_depth_limits", []):
            prefix = rule.get("prefix", "")
            if prefix and root.startswith(prefix) and max_depth > int(rule.get("max_depth", 1)):
                reason = rule.get("reason", "dive depth limit")
                return False, (
                    f"dive on '{root}' limited to depth {rule.get('max_depth',1)} "
                    f"({reason})"
                )

    if op == "eval" and SAFE_MODE:
        code = str(args.get("code", ""))
        for pat in BL.get("eval_code_blocked", []):
            try:
                if re.search(pat, code):
                    return False, (
                        f"eval code matches blacklisted pattern '{pat}'. "
                        "Disable safe_mode to override (POST /safe_mode {enabled:false})."
                    )
            except re.error:
                pass

    return True, None

app = Server("serotonin-bridge")

cmd_queue: asyncio.Queue[dict] = asyncio.Queue()
pending:   dict[str, asyncio.Future] = {}

_bridge_sem: asyncio.Semaphore | None = None

def _sem() -> asyncio.Semaphore:
    global _bridge_sem
    if _bridge_sem is None:
        _bridge_sem = asyncio.Semaphore(1)
    return _bridge_sem

async def bridge_call(op: str, args: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    async with _sem():
        cmd_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        pending[cmd_id] = fut
        await cmd_queue.put({"id": cmd_id, "op": op, "args": args or {}})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            pending.pop(cmd_id, None)
            raise RuntimeError(
                f"Bridge timeout after {timeout}s. Load bridge.lua in Serotonin Scripting tab."
            )

def _lua_literal(v: Any) -> str:
    if v is None:               return "nil"
    if isinstance(v, bool):     return "true" if v else "false"
    if isinstance(v, (int, float)): return repr(v)
    if isinstance(v, str):      return json.dumps(v)
    raise TypeError(f"cannot encode {type(v).__name__} as Lua literal")

async def http_poll(request: web.Request) -> web.Response:
    try:
        cmd = await asyncio.wait_for(cmd_queue.get(), timeout=POLL_HOLD_SECONDS)
    except asyncio.TimeoutError:
        return web.json_response([])
    return web.json_response([cmd])

async def http_result(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as e:
        return web.json_response({"error": f"bad json: {e}"}, status=400)
    cmd_id = data.get("id")
    fut = pending.pop(cmd_id, None)
    if fut and not fut.done():
        err = data.get("error")
        if err:
            fut.set_exception(RuntimeError(str(err)))
        else:
            fut.set_result(data.get("result"))
    return web.json_response({"ok": True})

async def http_health(request: web.Request) -> web.Response:
    return web.json_response({
        "queued":  cmd_queue.qsize(),
        "pending": len(pending),
    })

async def http_exec(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as e:
        return web.json_response({"error": f"bad json: {e}"}, status=400)
    op      = data.get("op")
    args    = data.get("args") or {}
    timeout = float(data.get("timeout", DEFAULT_TIMEOUT))
    if not isinstance(op, str):
        return web.json_response({"error": "missing 'op'"}, status=400)

    allowed, reason = check_request_safe(op, args)
    if not allowed:
        log.info("blocked by safe_mode: op=%s reason=%s", op, reason)
        return web.json_response(
            {"ok": False, "error": reason, "blocked_by": "crash_blacklist", "safe_mode": SAFE_MODE},
            status=400,
        )

    try:
        result = await bridge_call(op, args, timeout=timeout)
        return web.json_response({"ok": True, "result": result})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def http_safe_mode(request: web.Request) -> web.Response:
    global SAFE_MODE
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            data = {}
        if "enabled" in data:
            SAFE_MODE = bool(data["enabled"])
            log.info("safe_mode set to %s", SAFE_MODE)
    return web.json_response({
        "safe_mode":        SAFE_MODE,
        "blacklist_rules":  {
            "paths":              len(BL.get("paths", [])),
            "dive_depth_limits":  len(BL.get("dive_depth_limits", [])),
            "eval_code_blocked":  len(BL.get("eval_code_blocked", [])),
            "history":            len(BL.get("history", [])),
        },
    })

async def http_blacklist_get(request: web.Request) -> web.Response:
    return web.json_response({"safe_mode": SAFE_MODE, **BL})

async def http_blacklist_reload(request: web.Request) -> web.Response:
    global BL
    BL = load_blacklist()
    return web.json_response({"ok": True, "rules": {
        "paths":             len(BL.get("paths", [])),
        "dive_depth_limits": len(BL.get("dive_depth_limits", [])),
        "eval_code_blocked": len(BL.get("eval_code_blocked", [])),
    }})

async def http_blacklist_patch(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as e:
        return web.json_response({"error": f"bad json: {e}"}, status=400)

    added, removed = [], []
    for bucket in ("paths", "eval_code_blocked"):
        for v in (data.get("add") or {}).get(bucket, []):
            if v not in BL.get(bucket, []):
                BL.setdefault(bucket, []).append(v)
                added.append(f"{bucket}: {v}")
        for v in (data.get("remove") or {}).get(bucket, []):
            if v in BL.get(bucket, []):
                BL[bucket].remove(v)
                removed.append(f"{bucket}: {v}")

    for rule in (data.get("add") or {}).get("dive_depth_limits", []):
        prefix = rule.get("prefix")
        if not prefix:
            continue
        existing = [r for r in BL.get("dive_depth_limits", []) if r.get("prefix") == prefix]
        if existing:
            existing[0].update(rule)
        else:
            BL.setdefault("dive_depth_limits", []).append(rule)
        added.append(f"dive_depth_limits: {prefix}")
    for prefix in (data.get("remove") or {}).get("dive_depth_limits", []):
        before = len(BL.get("dive_depth_limits", []))
        BL["dive_depth_limits"] = [r for r in BL.get("dive_depth_limits", []) if r.get("prefix") != prefix]
        if len(BL["dive_depth_limits"]) < before:
            removed.append(f"dive_depth_limits: {prefix}")

    save_blacklist()
    return web.json_response({"ok": True, "added": added, "removed": removed})

async def http_crash_report(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as e:
        return web.json_response({"error": f"bad json: {e}"}, status=400)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "op":        data.get("last_op"),
        "args":      data.get("last_args"),
        "note":      data.get("note"),
    }
    BL.setdefault("history", []).append(entry)
    BL["history"] = BL["history"][-50:]

    auto_added: list[str] = []

    if entry["op"] == "dive":
        a    = entry["args"] or {}
        root = str(a.get("root", ""))
        md   = int(a.get("max_depth", 2))
        for prefix in ("game.Workspace.Live.",):
            if root.startswith(prefix) and md >= 2:
                already = any(r.get("prefix") == prefix for r in BL.get("dive_depth_limits", []))
                if not already:
                    BL.setdefault("dive_depth_limits", []).append({
                        "prefix": prefix, "max_depth": 1,
                        "reason": f"auto: crash reported at {entry['timestamp']}",
                    })
                    auto_added.append(f"dive_depth_limits: {prefix} max_depth=1")

    if entry["op"] in ("inspect", "safe_inspect"):
        target = str((entry["args"] or {}).get("target", ""))
        if target.startswith("game.") and target not in BL.get("paths", []):
            BL.setdefault("paths", []).append(target)
            auto_added.append(f"paths: {target}")

    if entry["op"] == "eval":
        code = str((entry["args"] or {}).get("code", "")).strip()
        m = re.fullmatch(r"return\s+(game\s*\.[\w\.]+)\s*;?", code)
        if m:
            pat = re.escape(m.group(1))
            if pat not in BL.get("eval_code_blocked", []):
                BL.setdefault("eval_code_blocked", []).append(pat)
                auto_added.append(f"eval_code_blocked: {pat}")

    save_blacklist()
    log.info("crash_report: %s auto_added=%s", entry["op"], auto_added)
    return web.json_response({"ok": True, "recorded": entry, "auto_added": auto_added})

async def http_cancel(request: web.Request) -> web.Response:
    dropped_q = 0
    while not cmd_queue.empty():
        try:
            cmd_queue.get_nowait()
            dropped_q += 1
        except asyncio.QueueEmpty:
            break
    dropped_p = 0
    for cmd_id, fut in list(pending.items()):
        if not fut.done():
            fut.set_exception(RuntimeError("cancelled"))
        pending.pop(cmd_id, None)
        dropped_p += 1
    log.info("cancel: dropped %d queued, %d pending", dropped_q, dropped_p)
    return web.json_response({"ok": True, "dropped_queued": dropped_q, "dropped_pending": dropped_p})

TOOLS: list[types.Tool] = [
    types.Tool(
        name="serotonin_ping",
        description="Ping the Lua bridge. Returns 'pong' if bridge.lua is loaded in Serotonin.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="serotonin_eval",
        description=(
            "Execute arbitrary Lua code in the Serotonin runtime. "
            "The code is wrapped as `return (function() <code> end)()`, so either "
            "`return <expr>` or just `<expr>` works to return a value. "
            "Result is serialized: Instances become {__type:'Instance', handle, ClassName, Name, Address}; "
            "Vector3 → {X,Y,Z}; Color3 → {R,G,B}. Handles are valid until the script is unloaded."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "code":     {"type": "string"},
                "maxdepth": {"type": "integer", "default": 4, "minimum": 1, "maximum": 8},
                "timeout":  {"type": "number", "default": 30.0},
            },
            "required": ["code"],
        },
    ),
    types.Tool(
        name="serotonin_inspect",
        description=(
            "Return a detailed view of one Instance: known properties, Attributes, "
            "Children (serialized), ChildrenCount, and a fresh handle. "
            "`target` is either a dot-path (e.g. 'game.Workspace') or a handle (e.g. 'h42')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target":       {"type": "string"},
                "max_children": {"type": "integer", "default": 100},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="serotonin_search_instances",
        description=(
            "Walk root:GetDescendants() and return Instances whose Name contains `pattern` "
            "(case-insensitive substring). Optional `class_name` filters by exact ClassName."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pattern":     {"type": "string", "default": ""},
                "class_name":  {"type": "string"},
                "root":        {"type": "string", "default": "game"},
                "max_results": {"type": "integer", "default": 100, "minimum": 1, "maximum": 2000},
            },
        },
    ),
    types.Tool(
        name="serotonin_tree",
        description="Return the child tree of `root` up to `max_depth` (default 2), each node with Name/ClassName.",
        inputSchema={
            "type": "object",
            "properties": {
                "root":         {"type": "string", "default": "game.Workspace"},
                "max_depth":    {"type": "integer", "default": 2, "minimum": 1, "maximum": 6},
                "max_children": {"type": "integer", "default": 30},
            },
        },
    ),
    types.Tool(
        name="serotonin_list_players",
        description=(
            "Return entity.GetPlayers() with cached per-player fields "
            "(Name, DisplayName, Team, Health, MaxHealth, Position, Velocity, "
            "IsAlive, IsEnemy, IsVisible, Weapon, BoundingBox, TeamColor). "
            "Uses pcall on each field so missing ones are silently omitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enemies_only": {"type": "boolean", "default": False},
            },
        },
    ),
    types.Tool(
        name="serotonin_list_parts",
        description=(
            "Return entity.GetParts() with Position/Size/Rotation for each cached part. "
            "Optionally filter by distance from `origin` within `radius`. "
            "Set `include_extras=true` to also include Address/ClassName/Primitive/Color/"
            "Transparency/Shape/MeshId/HasMesh per part (uses new entity.GetPart* methods)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "origin":         {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                "radius":         {"type": "number"},
                "max_results":    {"type": "integer", "default": 500},
                "include_extras": {"type": "boolean", "default": False},
            },
        },
    ),
    types.Tool(
        name="serotonin_parts_count",
        description=(
            "Return entity.GetPartsCount() — total number of cached parts. "
            "Cheap call, useful before deciding to enumerate via list_parts."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="serotonin_part_details",
        description=(
            "Full metadata for ONE cached part by its `index` (1-based, from entity.GetParts()). "
            "Returns Position/Size/Rotation + Address/ClassName/Primitive/Color/"
            "Transparency/Shape/MeshId/HasMesh + CubeVertices (8 corners of OBB). "
            "Each field is captured under pcall — missing methods are silently omitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 1},
            },
            "required": ["index"],
        },
    ),
    types.Tool(
        name="serotonin_get_bones",
        description="For a player index, fetch Position/Size/Rotation for a list of bones.",
        inputSchema={
            "type": "object",
            "properties": {
                "player_index": {"type": "integer"},
                "bones": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [
                        "Head", "UpperTorso", "LowerTorso", "HumanoidRootPart",
                        "LeftUpperArm", "RightUpperArm",
                        "LeftUpperLeg", "RightUpperLeg",
                    ],
                },
            },
            "required": ["player_index"],
        },
    ),
    types.Tool(
        name="serotonin_memory_read",
        description=(
            "memory.Read(type, address). Supported types (verified empirically): "
            "'byte', 'short', 'int64', 'uint64', 'float', 'double', 'bool', 'string', 'ptr', 'pointer'. "
            "Docs mention int8/int16/int32 but those are NOT accepted. "
            "For 32-bit reads, read as int64 and mask with % 0x100000000."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type":    {"type": "string"},
                "address": {"type": "integer"},
            },
            "required": ["type", "address"],
        },
    ),
    types.Tool(
        name="serotonin_memory_write",
        description="memory.Write(type, address, value). Same type list as memory_read.",
        inputSchema={
            "type": "object",
            "properties": {
                "type":    {"type": "string"},
                "address": {"type": "integer"},
                "value":   {},
            },
            "required": ["type", "address", "value"],
        },
    ),
    types.Tool(
        name="serotonin_memory_base",
        description="Return memory.GetBase() — base address of RobloxPlayerBeta.exe.",
        inputSchema={"type": "object", "properties": {}},
    ),

    types.Tool(
        name="serotonin_find_by_class",
        description=(
            "Return all descendants of `root` whose ClassName equals `class_name`. "
            "Much faster and more common than search_instances for type-based queries. "
            "Use this to find all Humanoids, Parts, LocalScripts, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "class_name": {"type": "string"},
                "root":       {"type": "string", "default": "game.Workspace",
                               "description": "Lua expression for root (e.g. game, game.Workspace, game:GetService('Players'))"},
                "limit":      {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
            },
            "required": ["class_name"],
        },
    ),
    types.Tool(
        name="serotonin_find_player_model",
        description=(
            "Find the player Model in game.Workspace.Live by Name. Returns the Model "
            "with children serialized. Useful when entity.GetPlayers() gives you a name "
            "but you need the live Workspace model (to read HumanoidRootPart, Tank parts, etc.)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="serotonin_nearest",
        description=(
            "Find the nearest descendant matching `class_name` (or any class if omitted) "
            "within `radius` studs of `origin` (or LocalPlayer HRP if omitted). "
            "Returns {instance, distance} or null."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "class_name": {"type": "string"},
                "origin":     {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                "radius":     {"type": "number"},
                "root":       {"type": "string", "default": "game.Workspace"},
            },
        },
    ),
    types.Tool(
        name="serotonin_descendants_stats",
        description=(
            "Return ClassName → count map for root:GetDescendants(), plus total. "
            "First call to understand what's in a container (e.g. 'what's in game.Workspace')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "root":  {"type": "string", "default": "game.Workspace"},
                "top_n": {"type": "integer", "default": 20},
            },
        },
    ),
    types.Tool(
        name="serotonin_get_scripts",
        description=(
            "List all Script / LocalScript / ModuleScript descendants of `root`, with their "
            "full dot-path from DataModel. Source code is NOT accessible via Serotonin API."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "root":  {"type": "string", "default": "game"},
                "limit": {"type": "integer", "default": 500},
            },
        },
    ),
    types.Tool(
        name="serotonin_players_full",
        description=(
            "Comprehensive per-player dump: all entity fields, live HumanoidRootPart world "
            "position (bone method), and screen projection. Prefer this over list_players."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enemies_only": {"type": "boolean", "default": False},
            },
        },
    ),
    types.Tool(
        name="serotonin_project_to_screen",
        description=(
            "utility.WorldToScreen(Vector3) — project a world position to 2D screen. "
            "Returns {x, y, on_screen}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
    ),
    types.Tool(
        name="serotonin_screen_info",
        description=(
            "Window/camera/input snapshot: window size, camera position, mouse position, "
            "delta time, tick count, menu open state."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),

    # ---- file ops (cheat sandbox at C:\Serotonin\files) -----------------
    types.Tool(
        name="serotonin_file_read",
        description="file.read(path). Returns the file contents as a string, or null if missing. Path resolves under the cheat sandbox unless absolute.",
        inputSchema={
            "type": "object",
            "properties": { "path": {"type": "string"} },
            "required": ["path"],
        },
    ),
    types.Tool(
        name="serotonin_file_write",
        description="file.write(path, content). Overwrites the file. Returns true on success, false if the parent directory is missing. Use serotonin_file_mkdir first for nested paths.",
        inputSchema={
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
                "append":  {"type": "boolean", "default": False, "description": "use file.append instead of file.write (creates the file if missing)"},
            },
            "required": ["path", "content"],
        },
    ),
    types.Tool(
        name="serotonin_file_listdir",
        description="file.listdir(path). Returns an array of {name, isDirectory, isFile, size?} records, or null for a missing directory. Pass empty string for the sandbox root.",
        inputSchema={
            "type": "object",
            "properties": { "path": {"type": "string", "default": ""} },
        },
    ),
    types.Tool(
        name="serotonin_file_op",
        description="One-shot file metadata op: 'exists' / 'isdir' / 'mkdir' (recursive) / 'delete'. Returns the boolean result of the underlying call.",
        inputSchema={
            "type": "object",
            "properties": {
                "op":   {"type": "string", "enum": ["exists", "isdir", "mkdir", "delete"]},
                "path": {"type": "string"},
            },
            "required": ["op", "path"],
        },
    ),

    # ---- memory.Scan -----------------------------------------------------
    types.Tool(
        name="serotonin_memory_scan",
        description="memory.Scan(pattern, [module]). Pattern is a hex AOB with '??' wildcards. With one arg returns the first absolute address as a number or nil. With a module string returns an array of all matches inside that module.",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "AOB pattern, e.g. '48 89 5C 24 ??'"},
                "module":  {"type": "string", "description": "optional, e.g. 'RobloxPlayerBeta.exe'"},
                "limit":   {"type": "integer", "default": 100, "description": "max addresses to return for the table form"},
            },
            "required": ["pattern"],
        },
    ),
    types.Tool(
        name="serotonin_memory_is_valid",
        description="memory.IsValid(address). Returns true if the virtual address is inside a readable page in the Roblox process.",
        inputSchema={
            "type": "object",
            "properties": { "address": {"type": "integer"} },
            "required": ["address"],
        },
    ),

    # ---- audio.Beep (safe), audio.StopAll --------------------------------
    types.Tool(
        name="serotonin_audio_beep",
        description="audio.Beep(freq_hz, duration_ms). Plays a system beep. Synchronous, blocks for duration_ms. PlaySound is intentionally not exposed because non-WAV input crashes the cheat.",
        inputSchema={
            "type": "object",
            "properties": {
                "freq_hz":     {"type": "integer", "minimum": 1, "maximum": 32000},
                "duration_ms": {"type": "integer", "minimum": 1, "maximum": 5000},
            },
            "required": ["freq_hz", "duration_ms"],
        },
    ),
    types.Tool(
        name="serotonin_audio_stop_all",
        description="audio.StopAll(). Silences every playing sound. Safe no-op when nothing is playing.",
        inputSchema={"type": "object", "properties": {}},
    ),

    # ---- ui state read/write ---------------------------------------------
    types.Tool(
        name="serotonin_ui_get_value",
        description="ui.GetValue(tab, container, label). Reads a widget's current value. Return type depends on the widget kind (bool / number / string / table).",
        inputSchema={
            "type": "object",
            "properties": {
                "tab":       {"type": "string"},
                "container": {"type": "string"},
                "label":     {"type": "string"},
            },
            "required": ["tab", "container", "label"],
        },
    ),
    types.Tool(
        name="serotonin_ui_set_value",
        description=(
            "ui.SetValue(tab, container, label, value). Value type must match the widget. "
            "Pass JSON for table values: Multiselect={'1':true,'2':false,...}, "
            "Colorpicker={'r':R,'g':G,'b':B,'a':A}, Hotkey=number (Windows VK code), "
            "Dropdown/Listbox=number (1-based index)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tab":       {"type": "string"},
                "container": {"type": "string"},
                "label":     {"type": "string"},
                "value":     {},
            },
            "required": ["tab", "container", "label", "value"],
        },
    ),

]

@app.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS

@app.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}
    try:
        result = await _dispatch(name, args)
    except Exception as e:
        log.warning("tool %s failed: %s", name, e)
        return [types.TextContent(type="text", text=f"ERROR: {e}")]
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    return [types.TextContent(type="text", text=text)]

async def _dispatch(name: str, a: dict) -> Any:
    if name == "serotonin_ping":
        return await bridge_call("ping", timeout=5.0)

    if name == "serotonin_eval":
        return await bridge_call(
            "eval",
            {"code": a["code"], "maxdepth": int(a.get("maxdepth", 4))},
            timeout=float(a.get("timeout", DEFAULT_TIMEOUT)),
        )

    if name == "serotonin_inspect":
        return await bridge_call(
            "inspect",
            {"target": a["target"], "max_children": int(a.get("max_children", 100))},
        )

    if name == "serotonin_search_instances":
        pattern     = a.get("pattern", "") or ""
        class_name  = a.get("class_name")
        root        = a.get("root", "game")
        max_results = int(a.get("max_results", 100))
        code = f"""
local root_val = {root}
if root_val == nil then error("root resolves to nil: {root}") end
local pat = string.lower({json.dumps(pattern)})
local cls_filter = {_lua_literal(class_name)}
local mx = {max_results}
local descendants = root_val:GetDescendants()
local out = {{}}
for i = 1, #descendants do
    if #out >= mx then break end
    local inst = descendants[i]
    local ok_n, nm = pcall(function() return inst.Name end)
    if ok_n and type(nm) == "string" then
        if pat == "" or string.find(string.lower(nm), pat, 1, true) then
            if cls_filter then
                local ok_c, cl = pcall(function() return inst.ClassName end)
                if ok_c and cl == cls_filter then out[#out+1] = inst end
            else
                out[#out+1] = inst
            end
        end
    end
end
return out
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 2})

    if name == "serotonin_tree":
        root         = a.get("root", "game.Workspace")
        max_depth    = int(a.get("max_depth", 2))
        max_children = int(a.get("max_children", 30))
        code = f"""
local function walk(inst, depth)
    local node = {{
        Name = (pcall(function() return inst.Name end) and inst.Name) or "?",
        ClassName = (pcall(function() return inst.ClassName end) and inst.ClassName) or "?",
    }}
    if depth <= 0 then return node end
    local ok, ch = pcall(function() return inst:GetChildren() end)
    if not ok or type(ch) ~= "table" then return node end
    node.Children = {{}}
    local lim = math.min(#ch, {max_children})
    for i = 1, lim do node.Children[i] = walk(ch[i], depth - 1) end
    if #ch > lim then node.Truncated = #ch - lim end
    return node
end
local root_val = {root}
if root_val == nil then error("root resolves to nil: {root}") end
return walk(root_val, {max_depth})
"""
        return await bridge_call("eval", {"code": code, "maxdepth": max_depth + 4})

    if name == "serotonin_list_players":
        enemies_only = bool(a.get("enemies_only", False))
        code = f"""
local players = entity.GetPlayers({("true" if enemies_only else "")})
if type(players) ~= "table" then return {{}} end
local fields = {{
    "Name","DisplayName","UserId","Team","Weapon","Health","MaxHealth",
    "IsAlive","IsEnemy","IsVisible","IsWhitelisted",
    "Position","Velocity","BoundingBox","TeamColor"
}}
local out = {{}}
for i, p in ipairs(players) do
    local rec = {{}}
    for _, f in ipairs(fields) do
        local ok, v = pcall(function() return p[f] end)
        if ok and v ~= nil then rec[f] = v end
    end
    -- Real-time world pos via bone method (entity.Position is often stale in FFA)
    local ok, v = pcall(function() return p:GetBonePosition("HumanoidRootPart") end)
    if ok and v ~= nil then rec.HRP = v end
    out[i] = rec
end
return out
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 4})

    if name == "serotonin_list_parts":
        origin         = a.get("origin")
        radius         = a.get("radius")
        max_results    = int(a.get("max_results", 500))
        include_extras = bool(a.get("include_extras", False))
        if origin is not None:
            ox, oy, oz = map(float, origin)
        else:
            ox = oy = oz = 0.0
        have_filter = origin is not None and radius is not None
        rad = float(radius) if radius is not None else 0.0
        code = f"""
local parts = entity.GetParts()
if type(parts) ~= "table" then return {{}} end
local ox, oy, oz = {ox}, {oy}, {oz}
local have = {("true" if have_filter else "false")}
local r2 = {rad * rad}
local mx = {max_results}
local extras = {("true" if include_extras else "false")}

local function take(fn, idx)
    if type(fn) ~= "function" then return nil end
    local ok, v = pcall(fn, idx)
    if ok then return v end
end
local function take_color(idx)
    if type(entity.GetPartColor) ~= "function" then return nil end
    local ok, c1, c2, c3 = pcall(entity.GetPartColor, idx)
    if not ok or c1 == nil then return nil end
    if type(c1) == "number" then
        return {{ R = c1, G = c2 or 0, B = c3 or 0 }}
    end
    if type(c1) == "userdata" then
        local okR, R = pcall(function() return c1.R end)
        local okG, G = pcall(function() return c1.G end)
        local okB, B = pcall(function() return c1.B end)
        if okR and okG and okB then return {{ R = R, G = G, B = B }} end
    end
end

if #parts > 0 and type(entity.getPartPosition) ~= "function" then
    return {{ error = "entity.getPartPosition not available in this Serotonin build", parts_count = #parts }}
end
local out = {{}}
for _, idx in ipairs(parts) do
    if #out >= mx then break end
    local okp, x, y, z = pcall(entity.getPartPosition, idx)
    if not okp then break end
    local include = true
    if have then
        local dx, dy, dz = x - ox, y - oy, z - oz
        include = (dx*dx + dy*dy + dz*dz) <= r2
    end
    if include then
        local oks, sx, sy, sz = pcall(entity.getPartSize, idx)
        local okr, rot = pcall(entity.getPartRotation, idx)
        local rec = {{
            Index = idx,
            Position = {{ X = x, Y = y, Z = z }},
            Size     = oks and {{ X = sx, Y = sy, Z = sz }} or nil,
            Rotation = okr and rot or nil,
        }}
        if extras then
            rec.Address      = take(entity.GetPartAddress,      idx)
            rec.ClassName    = take(entity.GetPartClassName,    idx)
            rec.Primitive    = take(entity.GetPartPrimitive,    idx)
            rec.Transparency = take(entity.GetPartTransparency, idx)
            rec.Shape        = take(entity.GetPartShape,        idx)
            rec.MeshId       = take(entity.GetPartMeshId,       idx)
            rec.HasMesh      = take(entity.GetPartHasMesh,      idx)
            rec.Color        = take_color(idx)
        end
        out[#out+1] = rec
    end
end
return out
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 4})

    if name == "serotonin_parts_count":
        code = """
if type(entity.GetPartsCount) ~= "function" then
    return { error = "entity.GetPartsCount not available — Roblox/Serotonin update required" }
end
local ok, n = pcall(entity.GetPartsCount)
if not ok then return { error = tostring(n) } end
return { Count = n }
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 2})

    if name == "serotonin_part_details":
        idx = int(a["index"])
        code = f"""
local idx = {idx}
local rec = {{ Index = idx }}

local function take(fn)
    if type(fn) ~= "function" then return nil end
    local ok, v = pcall(fn, idx)
    if ok then return v end
end

if type(entity.getPartPosition) == "function" then
    local okp, x, y, z = pcall(entity.getPartPosition, idx)
    if okp then rec.Position = {{ X = x, Y = y, Z = z }} end
end
if type(entity.getPartSize) == "function" then
    local oks, sx, sy, sz = pcall(entity.getPartSize, idx)
    if oks then rec.Size = {{ X = sx, Y = sy, Z = sz }} end
end
if type(entity.getPartRotation) == "function" then
    local okr, rot = pcall(entity.getPartRotation, idx)
    if okr then rec.Rotation = rot end
end

rec.Address      = take(entity.GetPartAddress)
rec.ClassName    = take(entity.GetPartClassName)
rec.Primitive    = take(entity.GetPartPrimitive)
rec.Transparency = take(entity.GetPartTransparency)
rec.Shape        = take(entity.GetPartShape)
rec.MeshId       = take(entity.GetPartMeshId)
rec.HasMesh      = take(entity.GetPartHasMesh)

if type(entity.GetPartColor) == "function" then
    local okc, c1, c2, c3 = pcall(entity.GetPartColor, idx)
    if okc and c1 ~= nil then
        if type(c1) == "number" then
            rec.Color = {{ R = c1, G = c2 or 0, B = c3 or 0 }}
        elseif type(c1) == "userdata" then
            local okR, R = pcall(function() return c1.R end)
            local okG, G = pcall(function() return c1.G end)
            local okB, B = pcall(function() return c1.B end)
            if okR and okG and okB then rec.Color = {{ R = R, G = G, B = B }} end
        end
    end
end

if type(entity.GetPartCubeVertices) == "function" then
    local okv, v = pcall(entity.GetPartCubeVertices, idx)
    if okv and v ~= nil then rec.CubeVertices = v end
end

if type(entity.GetPartsCount) == "function" then
    local okt, n = pcall(entity.GetPartsCount)
    if okt then rec.TotalParts = n end
end

return rec
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 4})

    if name == "serotonin_get_bones":
        idx   = int(a["player_index"])
        bones = a.get("bones") or [
            "Head","UpperTorso","LowerTorso","HumanoidRootPart",
            "LeftUpperArm","RightUpperArm","LeftUpperLeg","RightUpperLeg",
        ]
        bones_lua = "{" + ",".join(json.dumps(b) for b in bones) + "}"
        code = f"""
local players = entity.GetPlayers()
local p = players and players[{idx}]
if not p then return {{ error = "no player at index {idx}, have " .. tostring(players and #players or 0) }} end
local bones = {bones_lua}
local out = {{ PlayerName = (pcall(function() return p.Name end) and p.Name) or "?" }}
local list = {{}}
for _, b in ipairs(bones) do
    local entry = {{ Name = b }}
    local ok, v = pcall(function() return p:GetBonePosition(b) end)
    if ok and v ~= nil then entry.Position = v end
    local ok, v = pcall(function() return p:GetBoneSize(b) end)
    if ok and v ~= nil then entry.Size = v end
    local ok, v = pcall(function() return p:GetBoneRotation(b) end)
    if ok and v ~= nil then entry.Rotation = v end
    list[#list+1] = entry
end
out.Bones = list
return out
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 4})

    if name == "serotonin_memory_read":
        mtype = a["type"]
        addr  = int(a["address"])
        code  = f"return memory.Read({json.dumps(mtype)}, {addr})"
        return await bridge_call("eval", {"code": code})

    if name == "serotonin_memory_write":
        mtype = a["type"]
        addr  = int(a["address"])
        code  = f"memory.Write({json.dumps(mtype)}, {addr}, {_lua_literal(a['value'])}) return true"
        return await bridge_call("eval", {"code": code})

    if name == "serotonin_memory_base":
        return await bridge_call("eval", {"code": "return memory.GetBase()"})

    if name == "serotonin_find_by_class":
        cls   = json.dumps(a["class_name"])
        root  = a.get("root", "game.Workspace")
        limit = int(a.get("limit", 200))
        code  = f"return _sero_find_class({cls}, {root}, {limit})"
        return await bridge_call("eval", {"code": code, "maxdepth": 2})

    if name == "serotonin_find_player_model":
        code = f"""
local m = _sero_find_player({json.dumps(a['name'])})
if not m then return {{ found = false }} end
local kids = {{}}
for i, c in ipairs(m:GetChildren()) do
    kids[i] = {{ Name = c.Name, ClassName = c.ClassName }}
end
local hrp = m:FindFirstChild("HumanoidRootPart")
return {{
    found = true,
    Name = m.Name,
    ClassName = m.ClassName,
    Children = kids,
    HRP_Position = hrp and hrp.Position,
    HRP_Velocity = hrp and hrp.Velocity,
}}
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 3})

    if name == "serotonin_nearest":
        cls = json.dumps(a["class_name"]) if a.get("class_name") else "nil"
        origin = a.get("origin")
        if origin is not None:
            ox, oy, oz = map(float, origin)
            origin_expr = f"Vector3.new({ox}, {oy}, {oz})"
        else:
            origin_expr = "nil"
        rad = float(a["radius"]) if a.get("radius") is not None else "nil"
        root = a.get("root", "game.Workspace")
        code = f"return _sero_nearest({cls}, {origin_expr}, {rad}, {root})"
        return await bridge_call("eval", {"code": code, "maxdepth": 3})

    if name == "serotonin_descendants_stats":
        root  = a.get("root", "game.Workspace")
        top_n = int(a.get("top_n", 20))
        code  = f"return _sero_stats({root}, {top_n})"
        return await bridge_call("eval", {"code": code, "maxdepth": 3})

    if name == "serotonin_get_scripts":
        root  = a.get("root", "game")
        limit = int(a.get("limit", 500))
        code  = f"return _sero_scripts({root}, {limit})"
        return await bridge_call("eval", {"code": code, "maxdepth": 3})

    if name == "serotonin_players_full":
        enemies_only = "true" if a.get("enemies_only") else ""
        code = f"""
local players = entity.GetPlayers({enemies_only})
local out = {{}}
for i, p in ipairs(players) do out[i] = _sero_player_snapshot(p) end
return out
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 4})

    if name == "serotonin_project_to_screen":
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
        code = f"return _sero_project(Vector3.new({x}, {y}, {z}))"
        return await bridge_call("eval", {"code": code, "maxdepth": 2})

    if name == "serotonin_screen_info":
        code = """
local w, h = cheat.getWindowSize()
return {
    WindowSize = { W = w, H = h },
    CameraPos  = game.CameraPosition,
    MousePos   = utility.GetMousePos(),
    DeltaTime  = utility.GetDeltaTime(),
    TickCount  = utility.GetTickCount(),
    MenuOpen   = utility.GetMenuState(),
}
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 2})

    if name == "serotonin_file_read":
        code = f"return file.read({json.dumps(a['path'])})"
        return await bridge_call("eval", {"code": code})

    if name == "serotonin_file_write":
        fn = "append" if a.get("append") else "write"
        code = f"return file.{fn}({json.dumps(a['path'])}, {json.dumps(a['content'])})"
        return await bridge_call("eval", {"code": code})

    if name == "serotonin_file_listdir":
        path = a.get("path", "")
        code = f"return file.listdir({json.dumps(path)})"
        return await bridge_call("eval", {"code": code, "maxdepth": 3})

    if name == "serotonin_file_op":
        op   = a["op"]
        path = a["path"]
        code = f"return file.{op}({json.dumps(path)})"
        return await bridge_call("eval", {"code": code})

    if name == "serotonin_memory_scan":
        pattern = a["pattern"]
        module  = a.get("module")
        limit   = int(a.get("limit", 100))
        if module:
            code = f"""
local hits = memory.Scan({json.dumps(pattern)}, {json.dumps(module)})
if type(hits) ~= "table" then return hits end
local out = {{}}
for i = 1, math.min({limit}, #hits) do out[i] = hits[i] end
return {{ count = #hits, addresses = out }}
"""
        else:
            code = f"return memory.Scan({json.dumps(pattern)})"
        return await bridge_call("eval", {"code": code, "maxdepth": 2})

    if name == "serotonin_memory_is_valid":
        addr = int(a["address"])
        code = f"return memory.IsValid({addr})"
        return await bridge_call("eval", {"code": code})

    if name == "serotonin_audio_beep":
        freq = int(a["freq_hz"])
        dur  = int(a["duration_ms"])
        code = f"audio.Beep({freq}, {dur}) return true"
        return await bridge_call("eval", {"code": code, "timeout": dur / 1000.0 + 5})

    if name == "serotonin_audio_stop_all":
        return await bridge_call("eval", {"code": "audio.StopAll() return true"})

    if name == "serotonin_ui_get_value":
        code = f"return ui.GetValue({json.dumps(a['tab'])}, {json.dumps(a['container'])}, {json.dumps(a['label'])})"
        return await bridge_call("eval", {"code": code, "maxdepth": 3})

    if name == "serotonin_ui_set_value":
        value_lua = _lua_literal(a["value"])
        code = (
            f"ui.SetValue({json.dumps(a['tab'])}, {json.dumps(a['container'])}, "
            f"{json.dumps(a['label'])}, {value_lua}) return true"
        )
        return await bridge_call("eval", {"code": code})

    raise RuntimeError(f"unknown tool: {name}")

async def start_http_server() -> web.AppRunner:
    http_app = web.Application()
    http_app.router.add_get ("/poll",          http_poll)
    http_app.router.add_post("/result",        http_result)
    http_app.router.add_get ("/health",        http_health)
    http_app.router.add_post("/exec",          http_exec)
    http_app.router.add_post("/cancel",        http_cancel)
    http_app.router.add_get ("/safe_mode",     http_safe_mode)
    http_app.router.add_post("/safe_mode",     http_safe_mode)
    http_app.router.add_get ("/blacklist",         http_blacklist_get)
    http_app.router.add_post("/blacklist",         http_blacklist_patch)
    http_app.router.add_post("/blacklist/reload",  http_blacklist_reload)
    http_app.router.add_post("/crash_report",      http_crash_report)
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    log.info("HTTP coordinator listening on http://%s:%d", HTTP_HOST, HTTP_PORT)
    return runner

async def main() -> None:
    global BL
    BL = load_blacklist()
    log.info(
        "blacklist loaded: %d paths, %d dive rules, %d eval patterns, %d history",
        len(BL.get("paths", [])),
        len(BL.get("dive_depth_limits", [])),
        len(BL.get("eval_code_blocked", [])),
        len(BL.get("history", [])),
    )
    runner = await start_http_server()
    try:
        if os.environ.get("SEROTONIN_HTTP_ONLY") == "1":
            log.info("HTTP-only mode — MCP stdio disabled, idling forever")
            while True:
                await asyncio.sleep(3600)
        async with stdio_server() as (read_stream, write_stream):
            log.info("MCP server ready on stdio")
            await app.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name    = "serotonin-bridge",
                    server_version = "0.1.0",
                    capabilities   = app.get_capabilities(
                        notification_options = NotificationOptions(),
                        experimental_capabilities = {},
                    ),
                ),
            )
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
