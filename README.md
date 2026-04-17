# mcp-serotonin

An MCP bridge between any MCP-capable agent and the [Serotonin](https://serotonin-1.gitbook.io/serotonin-docs) Lua runtime.

I wrote this because poking at Roblox games through Serotonin blind - guessing instance names, hoping `entity.GetPlayers` works in this particular mode - got old fast. With the bridge loaded, an agent can just look at the game: walk the workspace, list live players with their world positions, find the nearest Humanoid, read memory, project coordinates to screen. Then it writes Lua that actually works in that specific mode instead of generic ESP templates.

## How it works

```
MCP client  <-- stdio -->  server.py  <-- HTTP :8765 -->  bridge.lua  <-->  Serotonin  <-->  Roblox
```

`bridge.lua` runs inside Serotonin and long-polls the Python coordinator. When a tool call arrives, the Lua side executes it, serializes the result (Instances become handles you can pass back; Vector3/Color3 keep their types), and posts it back. An asyncio semaphore + one-at-a-time polling means parallel MCP calls can't stack parallel evals inside Serotonin - that path crashes the cheat reliably.

## Requirements

- Windows 10/11 + Serotonin
- Python 3.10+
- `mcp`, `aiohttp` (see `requirements.txt`)

## Install

```bash
git clone https://github.com/<you>/mcp-serotonin.git
cd mcp-serotonin
pip install -r requirements.txt
```

Drop `bridge.lua` into your Serotonin scripts folder - usually:

```
C:\Serotonin\scripts\bridge.lua
```

(The Scripting tab has an "Open Scripts Folder" button.)

## Hook it up to your MCP client

The server speaks stdio, so the config shape is the same for every client. Point it at `python C:/path/to/mcp-serotonin/server.py`.

Most clients read a JSON file that looks like:

```json
{
  "mcpServers": {
    "serotonin-bridge": {
      "command": "python",
      "args": ["C:/path/to/mcp-serotonin/server.py"],
      "env": { "PYTHONUNBUFFERED": "1" }
    }
  }
}
```

Put it wherever your client expects it (project-local `.mcp.json`, user-level config, IDE settings). `.mcp.json.example` in this repo is the same file, ready to copy.

## Run it

1. Launch Roblox + Serotonin.
2. In the Scripting tab, **Load** `bridge.lua`. You should see:
   ```
   [serotonin-bridge] loaded, polling http://127.0.0.1:8765
   ```
3. Start your MCP client - it'll spawn `server.py` over stdio on demand.
4. Call `serotonin_ping`. If you get `"pong"`, you're done.

If it times out, the bridge isn't running or can't reach `127.0.0.1:8765`. Reload the Lua script, check the cheat console for errors.

## Tools

| Tool | What it does |
|---|---|
| `serotonin_ping` | Liveness check. |
| `serotonin_eval` | Run arbitrary Lua. Instances / Vector3 / Color3 get serialized automatically. |
| `serotonin_inspect` | Properties, Attributes, Children for one Instance. Takes a dot-path or a handle. |
| `serotonin_search_instances` | Walk `GetDescendants` with Name substring + optional ClassName filter. |
| `serotonin_tree` | Recursive Name/ClassName dump up to N levels. |
| `serotonin_find_by_class` | All descendants of a specific ClassName. |
| `serotonin_find_player_model` | Find a player Model in `Workspace.Live` by Name, with its children + HRP. |
| `serotonin_nearest` | Nearest instance of a class within a radius. Origin defaults to LocalPlayer. |
| `serotonin_descendants_stats` | ClassName histogram for a subtree. |
| `serotonin_get_scripts` | All `Script`/`LocalScript`/`ModuleScript` with dot-paths. Source isn't exposed. |
| `serotonin_list_players` | `entity.GetPlayers()` + cached fields. |
| `serotonin_players_full` | Entity fields + live HumanoidRootPart + screen projection. Prefer this. |
| `serotonin_list_parts` | `entity.GetParts()` with optional radius filter. |
| `serotonin_get_bones` | Position/Size/Rotation for named bones of a player index. |
| `serotonin_project_to_screen` | `utility.WorldToScreen` for a Vector3. |
| `serotonin_screen_info` | Window size, camera, mouse, delta time, menu state. |
| `serotonin_memory_read` | `memory.Read(type, addr)`. |
| `serotonin_memory_write` | `memory.Write(type, addr, value)`. |
| `serotonin_memory_base` | `memory.GetBase()`. |

## A few things that bit me

**Memory types.** The docs list `int8/16/32` etc. - those are lies. Actually accepted: `byte`, `short`, `int64`, `uint64`, `float`, `double`, `bool`, `string`, `ptr`, `pointer`. For a 32-bit read, grab `int64` and mask with `% 0x100000000`.

**`_G` doesn't exist.** Serotonin runs Lua in a sandbox where `_G` is nil. Use `getfenv(1)` if you need the env table. `resolve_target` in `bridge.lua` already does this.

**Entity API returns userdata, not indices.** Docs say `entity.GetPlayers()` returns integers. It returns userdata objects. Access fields as `p.Name`, `p.Health`; call bone methods as `p:GetBonePosition("HumanoidRootPart")`. Indexing `entity.Name(idx)` doesn't work.

**`entity.Position` is often stale.** In FFA / Tank-style modes the cached position stays at `(0,0,0)`. Use `p:GetBonePosition("HumanoidRootPart")` for the live value. `serotonin_players_full` does this for you.

**Don't touch `game.PlaceID` or `game.GetFFlag`.** They crash the cheat on at least some builds. They're deliberately not exposed as tools - if you really need them, go through `serotonin_eval` at your own risk.

**Don't parallelize eval.** Two simultaneous evals crash Serotonin too. The server enforces serial execution end-to-end via a semaphore and single-command polls, so you're safe if you only go through the provided tools. If you bypass with `eval`, keep it sequential.

## Configuration

Env vars:

- `SEROTONIN_HTTP_HOST` (default `127.0.0.1`)
- `SEROTONIN_HTTP_PORT` (default `8765`)
- `SEROTONIN_HTTP_ONLY=1` - start only the HTTP coordinator, skip stdio MCP. Useful for debugging with `curl`.

Tunables in `bridge.lua` (top of file, `CFG` table):

- `base_url` - must match the host/port above
- `poll_interval_ms` - minimum gap between polls
- `inflight_ttl_ms` - watchdog reset if `http.Get` callback never fires
- `max_depth` - default serialization depth

## License

MIT.
