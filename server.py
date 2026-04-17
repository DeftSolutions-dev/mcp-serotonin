from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
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

HTTP_HOST         = os.environ.get("SEROTONIN_HTTP_HOST", "127.0.0.1")
HTTP_PORT         = int(os.environ.get("SEROTONIN_HTTP_PORT", "8765"))
POLL_HOLD_SECONDS = 9.0
DEFAULT_TIMEOUT   = 30.0

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
            "Code is wrapped as `return (function() <code> end)()`, so `return <expr>` "
            "or a bare expression both return a value. "
            "Instances serialize to {__type:'Instance', handle, ClassName, Name, Address}; "
            "Vector3 -> {X,Y,Z}; Color3 -> {R,G,B}."
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
            "Return properties, Attributes, Children, and ChildrenCount of one Instance. "
            "`target` is a dot-path (e.g. 'game.Workspace.Live.Player1') or a handle (e.g. 'h42')."
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
        description="Return the child tree of `root` up to `max_depth`, each node with Name/ClassName.",
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
            "Return entity.GetPlayers() with cached per-player fields. "
            "For live positions use serotonin_players_full instead."
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
            "Optionally filter by distance from `origin` within `radius`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "origin":      {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                "radius":      {"type": "number"},
                "max_results": {"type": "integer", "default": 500},
            },
        },
    ),
    types.Tool(
        name="serotonin_get_bones",
        description="For a player index, return Position/Size/Rotation for a list of bones.",
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
            "memory.Read(type, address). Accepted types (verified): "
            "byte, short, int64, uint64, float, double, bool, string, ptr, pointer. "
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
        description="Return memory.GetBase() - base address of RobloxPlayerBeta.exe.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="serotonin_find_by_class",
        description="Return all descendants of `root` whose ClassName equals `class_name`.",
        inputSchema={
            "type": "object",
            "properties": {
                "class_name": {"type": "string"},
                "root":       {"type": "string", "default": "game.Workspace"},
                "limit":      {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
            },
            "required": ["class_name"],
        },
    ),
    types.Tool(
        name="serotonin_find_player_model",
        description=(
            "Find the player Model in game.Workspace.Live by Name. Returns the Model "
            "with children and HumanoidRootPart position/velocity."
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
            "Find the nearest descendant matching `class_name` (or any) within `radius` studs "
            "of `origin` (or LocalPlayer HRP). Returns {instance, distance} or null."
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
        description="Return ClassName -> count map for root:GetDescendants(), plus total.",
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
            "List all Script / LocalScript / ModuleScript descendants of `root` with their dot-path. "
            "Source code is not exposed by the Serotonin API."
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
            "Per-player dump: all entity fields, live HumanoidRootPart world position, "
            "and screen projection. Prefer this over list_players."
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
        description="utility.WorldToScreen(Vector3) - project a world position to 2D. Returns {x, y, on_screen}.",
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
        description="Window/camera/input snapshot: window size, camera pos, mouse pos, delta time, tick, menu open.",
        inputSchema={"type": "object", "properties": {}},
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
    local ok, v = pcall(function() return p:GetBonePosition("HumanoidRootPart") end)
    if ok and v ~= nil then rec.HRP = v end
    out[i] = rec
end
return out
"""
        return await bridge_call("eval", {"code": code, "maxdepth": 4})

    if name == "serotonin_list_parts":
        origin      = a.get("origin")
        radius      = a.get("radius")
        max_results = int(a.get("max_results", 500))
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
local out = {{}}
for _, idx in ipairs(parts) do
    if #out >= mx then break end
    local x, y, z = entity.getPartPosition(idx)
    local include = true
    if have then
        local dx, dy, dz = x - ox, y - oy, z - oz
        include = (dx*dx + dy*dy + dz*dz) <= r2
    end
    if include then
        local sx, sy, sz = entity.getPartSize(idx)
        local rot = entity.getPartRotation(idx)
        out[#out+1] = {{
            Index = idx,
            Position = {{ X = x, Y = y, Z = z }},
            Size     = {{ X = sx, Y = sy, Z = sz }},
            Rotation = rot,
        }}
    end
end
return out
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
if not p then return {{ error = "no player at index {idx}" }} end
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

    raise RuntimeError(f"unknown tool: {name}")


async def start_http_server() -> web.AppRunner:
    http_app = web.Application()
    http_app.router.add_get ("/poll",   http_poll)
    http_app.router.add_post("/result", http_result)
    http_app.router.add_get ("/health", http_health)
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    log.info("HTTP coordinator listening on http://%s:%d", HTTP_HOST, HTTP_PORT)
    return runner


async def main() -> None:
    runner = await start_http_server()
    try:
        if os.environ.get("SEROTONIN_HTTP_ONLY") == "1":
            log.info("HTTP-only mode - MCP stdio disabled")
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
                        notification_options     = NotificationOptions(),
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
