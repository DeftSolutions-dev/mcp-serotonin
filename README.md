# mcp-serotonin

An MCP bridge between any MCP-capable agent and the [Serotonin](https://serotonin-1.gitbook.io/serotonin-docs) Lua runtime.

I wrote this because poking at Roblox games through Serotonin blind - guessing instance names, hoping `entity.GetPlayers` works in this particular mode - got old fast. With the bridge loaded, an agent can just look at the game: walk the workspace, list live players with their world positions, find the nearest Humanoid, read memory, project coordinates to screen. Then it writes Lua that actually works in that specific mode instead of generic ESP templates.

## How it works

```
MCP client  <-- stdio -->  server.py  <-- HTTP :8765 -->  bridge.lua  <-->  Serotonin  <-->  Roblox
```

`bridge.lua` runs inside Serotonin and long-polls the Python coordinator. When a tool call arrives, the Lua side executes it, serializes the result (Instances become handles you can pass back; Vector3/Color3 keep their types), and posts it back. An asyncio semaphore + one-at-a-time polling means parallel MCP calls can't stack parallel evals inside Serotonin - that path crashes the cheat reliably.

## Crash protection

Some Lua expressions in Serotonin trigger native C++ exceptions that `pcall` can't catch - they kill the cheat DLL. Reading `_G`, `game.DataModel`, `game.PlaceID`, `game.LocalPlayer.Backpack`, calling `Color3:ToHSV()` - all confirmed crashers. This release ships with:

- A **safe-mode pre-flight** in `server.py` that checks every op against `crash_blacklist.json` before it leaves the Python process. In safe mode (default: on) blocked ops never reach the cheat.
- A **class-based property allowlist** in `bridge.lua` - only documented properties are read via `safe_inspect` / `dive`. Undocumented fields are a known crash vector (Serotonin's proxy tries to resolve them via raw memory and faults on unknown offsets).
- A **`/crash_report` endpoint** that auto-extracts blacklist rules when you feed it the last-known-bad op. Learn once, never repeat.

`crash_blacklist.json` is at **version 2** in this release. The new entries (over the v1 baseline) are:
- `audio.PlaySound` with any string short enough to be obvious bogus or `nil`. Verified to crash the cheat with a native SEH exception.
- `cheat.LoadString` (any case). Every invocation we tried raised `"C++ exception"` in build `version-2e6461290a3541f5`.

If you hit a new crasher, POST it to `/crash_report` and it gets persisted.

## Requirements

- Windows 10/11 + Serotonin
- Python 3.10+
- `mcp`, `aiohttp` (see `requirements.txt`)

## Install

```bash
git clone https://github.com/DeftSolutions-dev/mcp-serotonin.git
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
   [serotonin-bridge v2] loaded, polling http://127.0.0.1:8765
   [serotonin-bridge v2] ops: ping eval inspect safe_inspect snapshot dive live_dump class_counts list_scripts search
   ```
3. Start your MCP client - it'll spawn `server.py` over stdio on demand.
4. Call `serotonin_ping`. If you get `"pong"`, you're done.

If it times out, the bridge isn't running or can't reach `127.0.0.1:8765`. Reload the Lua script, check the cheat console for errors.

## Tools (30 wrappers)

### Instance / world exploration

| Tool | What it does |
|---|---|
| `serotonin_ping` | Liveness check. |
| `serotonin_eval` | Run arbitrary Lua. Instances / Vector3 / Color3 get serialized automatically. Blocked patterns don't reach the cheat in safe mode. |
| `serotonin_inspect` | Properties, Attributes, Children for one Instance. Takes a dot-path or a handle. |
| `serotonin_search_instances` | Walk `GetDescendants` with Name substring + optional ClassName filter. |
| `serotonin_tree` | Recursive Name/ClassName dump up to N levels. |
| `serotonin_find_by_class` | All descendants of a specific ClassName. |
| `serotonin_find_player_model` | Find a player Model in `Workspace.Live` by Name, with its children + HRP. |
| `serotonin_nearest` | Nearest instance of a class within a radius. Origin defaults to LocalPlayer. |
| `serotonin_descendants_stats` | ClassName histogram for a subtree. |
| `serotonin_get_scripts` | All `Script`/`LocalScript`/`ModuleScript` with dot-paths. Source isn't exposed. |

### Entity / parts / players

| Tool | What it does |
|---|---|
| `serotonin_list_players` | `entity.GetPlayers()` + cached fields. |
| `serotonin_players_full` | Entity fields + live HumanoidRootPart + screen projection. Prefer this. |
| `serotonin_list_parts` | `entity.GetParts()` with optional radius filter. Set `include_extras=true` for Address/ClassName/Primitive/Color/Transparency/Shape/MeshId/HasMesh per part. |
| `serotonin_parts_count` | `entity.GetPartsCount()`. Cheap total count of cached parts. |
| `serotonin_part_details` | Full per-part dump for one index: pos/size/rot + extras + `GetPartCubeVertices` (8 OBB corners). |
| `serotonin_get_bones` | Position/Size/Rotation for named bones of a player index. |

### Screen / projection

| Tool | What it does |
|---|---|
| `serotonin_project_to_screen` | `utility.WorldToScreen` for a Vector3. |
| `serotonin_screen_info` | Window size, camera, mouse, delta time, menu state. |

### Memory

| Tool | What it does |
|---|---|
| `serotonin_memory_read` | `memory.Read(type, addr)`. |
| `serotonin_memory_write` | `memory.Write(type, addr, value)`. |
| `serotonin_memory_base` | `memory.GetBase()`. |
| `serotonin_memory_scan` | `memory.Scan(pattern, [module])`. AOB pattern with `??` wildcards. Returns first absolute address (1-arg form) or array of all matches inside the named module. |
| `serotonin_memory_is_valid` | `memory.IsValid(addr)`. True if the virtual address sits inside a readable page in the Roblox process. |

### File sandbox (`C:\Serotonin\files`)

| Tool | What it does |
|---|---|
| `serotonin_file_read` | `file.read(path)`. Returns the file contents as a string, or null when the file is missing. |
| `serotonin_file_write` | `file.write(path, content)` (default) or `file.append` when `append: true`. Returns true on success, false when the parent directory is missing. |
| `serotonin_file_listdir` | `file.listdir(path)`. Returns array of `{name, isDirectory, isFile, size?}` records. Pass empty string for sandbox root. |
| `serotonin_file_op` | One-shot metadata op: `exists` / `isdir` / `mkdir` (recursive) / `delete`. |

### Audio (safe subset)

| Tool | What it does |
|---|---|
| `serotonin_audio_beep` | `audio.Beep(freq, ms)`. Synchronous, blocks for `ms`. |
| `serotonin_audio_stop_all` | `audio.StopAll()`. Silences every playing sound. |

`audio.PlaySound` is intentionally **not** wrapped because non-WAV input crashes the cheat with a native SEH exception. Drive `PlaySound` through `serotonin_eval` only when you control the bytes (e.g. `file.read` of a known-good `.wav`).

### UI (drive the cheat menu)

| Tool | What it does |
|---|---|
| `serotonin_ui_get_value` | `ui.GetValue(tab, container, label)`. Reads a widget's current value (type depends on the widget kind). |
| `serotonin_ui_set_value` | `ui.SetValue(tab, container, label, value)`. Value must match the widget kind: bool / number / string / `{r,g,b,a}` / 1-based index / VK code, etc. |

## HTTP endpoints (for shell / debugging)

On top of the MCP tools, `server.py` exposes a few HTTP routes for when you want to drive the bridge directly with `curl` or another script:

| Method | Path | Purpose |
|---|---|---|
| POST | `/exec` | Run one op (`{op, args, timeout}`). Pre-flight checked against the blacklist. |
| POST | `/cancel` | Drop every queued command and cancel every pending future. Use after a crash. |
| GET / POST | `/safe_mode` | Get or toggle (`{enabled: true/false}`). |
| GET | `/blacklist` | Full blacklist dump. |
| POST | `/blacklist` | Patch (`{add: {paths, dive_depth_limits, eval_code_blocked}, remove: {...}}`). |
| POST | `/blacklist/reload` | Re-read `crash_blacklist.json` from disk. |
| POST | `/crash_report` | Report a crash (`{last_op, last_args, note}`). Auto-extracts rules for known crash shapes. |
| GET | `/health` | Queue depth. |

## Things that bite (and how this release handles them)

**Memory types.** The verified accepted types are: `byte`, `short`, `ushort`, `int`, `uint`, `int64`, `uint64`, `float`, `double`, `bool`, `string`, `ptr`, `pointer`, `vector2`, `vector3`, `color3`, `cframe` (read-only). All 13 numeric/pointer types tested working against `memory.GetBase()`. The old `int8/16/32` shortcuts are not accepted.

**`_G` is a native crasher.** Not just nil - even `type(_G)` inside `pcall` takes down the DLL. Blacklisted as regex `\b_G\b`. Use `getfenv(1)` if you need the env table.

**`game.GetService` uses dot syntax, not colon.** `game.GetService("Players")` works; `game:GetService(...)` returns a `calling 'GetService' on NN` error. The Lua `game` is a sandbox proxy table, not an Instance userdata - `:IsDescendantOf` / `:IsAncestorOf` fail on it for the same reason.

**Entity API returns userdata, not indices.** Docs say `entity.GetPlayers()` returns integers. It returns userdata objects. Access fields as `p.Name`, `p.Health`; call bone methods as `p:GetBonePosition("HumanoidRootPart")`.

**`entity.Position` is often stale.** In FFA / Tank-style modes the cached position stays at `(0,0,0)`. Use `p:GetBonePosition("HumanoidRootPart")` for the live value. `serotonin_players_full` does this for you.

**Documented-but-broken.** `Vector3:FuzzyEq` doesn't exist (Lua error). `Color3:ToHSV()` crashes (native). `game.GetFFlag` / `game.SetFFlag` crash. Blacklisted regardless of what the docs say.

**`audio.PlaySound` crashes on any non-WAV string.** Verified with `""`, `"not-wav"`, single-byte `"x"`. The internal WAV loader does not validate the RIFF header before walking it, so a bad string triggers a native SEH crash that `pcall` cannot catch. Blacklisted as `audio\.PlaySound\s*\(\s*["'][^"']{0,10}["']` plus a `nil`-arg variant. Pass real WAV bytes from `file.read` or `http.Get` only.

**`cheat.LoadString` is broken in build `version-2e6461290a3541f5`.** Every 2-arg invocation we tried, including syntactically valid Lua like `("name", "x = 1")`, raised `"C++ exception"`. Blacklisted. Use the standard Lua `loadstring` / `load` functions instead, they work cleanly in the sandbox.

**`raknet` is vestigial.** `raknet.is_connected()` returns `false` even on a live server, and callbacks added through `raknet.add_send_hook` never fire. Roblox abandoned RakNet around 2015 in favor of an ENet-derived stack; the API surface remains for backward compatibility but observes no packets. Hook at a different layer (memory patches, RemoteEvent interface) for traffic capture.

**Undocumented LocalPlayer fields crash.** `game.LocalPlayer.Backpack` is a confirmed native crasher. `PlayerGui`, `PlayerScripts`, `StarterGear`, `AccountAge`, `FollowUserId`, and two dozen other undocumented Player properties are blacklisted by a single regex. If you truly need one, turn off safe_mode, disable the pattern, and take the risk.

**Don't parallelize eval.** Two simultaneous evals crash Serotonin. The server enforces serial execution via a semaphore; stay on the tools and you're safe.

## Configuration

Env vars:

- `SEROTONIN_HTTP_HOST` (default `127.0.0.1`)
- `SEROTONIN_HTTP_PORT` (default `8765`)
- `SEROTONIN_HTTP_ONLY=1` - start only the HTTP coordinator, skip stdio MCP. Useful for debugging with `curl`.

Tunables in `bridge.lua` (top of file, `CFG` table):

- `base_url` - must match the host/port above
- `poll_interval_ms` - minimum gap between polls (default 100)
- `inflight_ttl_ms` - watchdog reset if `http.Get` callback never fires (default 12000)
- `max_depth` - default serialization depth (default 3)

Timeouts are synchronized: poll hold (9s) < server default timeout (10s) < bridge inflight TTL (12s). Don't break this ordering or the watchdog will race the client and you'll get phantom resets.

## License

MIT.
