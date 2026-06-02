# mcp-serotonin

## What is this
Tired of writing ESP blind, guessing whether `entity.GetParts` works in this mode, or watching the Cheat crash because `game.PlaceID` is apparently cursed?

This is an MCP server that bridges any MCP-capable LLM agent (Claude Code, Cursor, Cline, Continue…) to the Serotonin Lua runtime. The agent **sees the live game** - walks the Workspace tree, reads bones and positions, projects world coords to screen, reads memory - then writes Lua tailored to your mode instead of generic templates.

As of **v0.3.0** the transport is **file-based IPC**, so it keeps working with the Serotonin menu **closed** - no more "open the menu or nothing fires", no more stalls under load.

## Why this instead of Studio / debuggers

```
Roblox Studio     - can't attach to a live public server, only your own place files

Ghidra / x64dbg   - see bytes, not game objects; reverse the whole tree before asking
                    "who's alive?"

CheatEngine       - scans values, can't walk the instance graph or draw overlays

Script executors  - give you Lua, but you write blind and retry on crash
```
**mcp-serotonin** gives an LLM agent live access to all of that at once - tree, entity snapshot, bones, screen projection, memory, arbitrary Lua. The agent verifies what's in the mode before coding, tests hypotheses with `eval`, and ships scripts that work on the first load.

## How it works

```
MCP client  <-- stdio -->  server.py  <-- files -->  bridge.lua  <-->  Serotonin  <-->  Roblox
                                       agent/cmd.json
                                       agent/result.json
```

`server.py` writes the next command to `C:\Serotonin\files\agent\cmd.json` (atomically - temp + replace), `bridge.lua` reads it on its `onUpdate` frame, runs it on the game thread, serializes the result (Instances become handles you can pass back; Vector3/Color3 keep their types) and writes `agent/result.json`. One command at a time, serialized end-to-end - parallel evals stacking inside Serotonin crash the Cheat reliably. No sockets and no async HTTP callbacks: Serotonin only pumps HTTP callbacks while the menu renders, so the old transport stalled with the menu closed; synchronous file IO on `onUpdate` does not.

## Crash protection

Some Lua expressions in Serotonin trigger native C++ exceptions that `pcall` can't catch - they kill the Cheat DLL. Reading `_G`, `game.DataModel`, `game.PlaceID`, `game.LocalPlayer.Backpack`, calling `Color3:ToHSV()` - all confirmed crashers. This release ships with:

- A **safe-mode pre-flight** in `server.py` that checks every op against `crash_blacklist.json` before it leaves the Python process. In safe mode (default: on) blocked ops never reach the Cheat.
- A **class-based property allowlist** in `bridge.lua` - only documented properties are read via `safe_inspect` / `dive`. Undocumented fields are a known crash vector (Serotonin's proxy tries to resolve them via raw memory and faults on unknown offsets).
- A **per-op read/time budget** plus a `HEAVY_SKIP` subtree list, so a tree walk never materialises the whole `GetDescendants` graph in one native call (the old AV / frame-stall path on big trees).
- A **`/crash_report` endpoint** that auto-extracts blacklist rules when you feed it the last-known-bad op. Learn once, never repeat.

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
   [serotonin-bridge v3] file-IPC loaded - agent/cmd.json <-> agent/result.json (menu-independent)
   ```
3. Start your MCP client - it'll spawn `server.py` over stdio on demand.
4. Call `serotonin_ping`. If you get `"pong"`, you're done. **The Serotonin menu can stay closed** - the bridge runs off `onUpdate`, not the menu render loop.

If it times out, the bridge isn't loaded. Reload the Lua script and check the Cheat console for errors.

## Tools (31 wrappers)

### Instance / world exploration

| Tool | What it does |
|---|---|
| `serotonin_ping` | Liveness check. |
| `serotonin_eval` | Run arbitrary Lua. Instances / Vector3 / Color3 get serialized automatically. Blocked patterns don't reach the Cheat in safe mode. |
| `serotonin_inspect` | Properties, Attributes, Children for one Instance. Takes a dot-path or a handle. |
| `serotonin_search_instances` | Walk descendants with Name substring + optional ClassName filter (bounded, skips crash-prone subtrees). |
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

`audio.PlaySound` is intentionally **not** wrapped because non-WAV input crashes the Cheat with a native SEH exception. Drive `PlaySound` through `serotonin_eval` only when you control the bytes (e.g. `file.read` of a known-good `.wav`).

### UI (drive the Cheat menu)

| Tool | What it does |
|---|---|
| `serotonin_ui_get_value` | `ui.GetValue(tab, container, label)`. Reads a widget's current value (type depends on the widget kind). |
| `serotonin_ui_set_value` | `ui.SetValue(tab, container, label, value)`. Value must match the widget kind: bool / number / string / `{r,g,b,a}` / 1-based index / VK code, etc. |

## HTTP endpoints (for shell / debugging)

The file channel is the transport. On top of the MCP tools, `server.py` still runs a small HTTP server so you can drive the bridge directly with `curl` (it shares the same file IPC underneath):

| Method | Path | Purpose |
|---|---|---|
| POST | `/exec` | Run one op (`{op, args, timeout}`). Pre-flight checked against the blacklist. |
| GET / POST | `/safe_mode` | Get or toggle (`{enabled: true/false}`). |
| GET | `/blacklist` | Full blacklist dump. |
| POST | `/blacklist` | Patch (`{add: {paths, dive_depth_limits, eval_code_blocked}, remove: {...}}`). |
| POST | `/blacklist/reload` | Re-read `crash_blacklist.json` from disk. |
| POST | `/crash_report` | Report a crash (`{last_op, last_args, note}`). Auto-extracts rules for known crash shapes. |
| GET | `/health` | Server status. |

Set `SEROTONIN_HTTP_ONLY=1` to start only the HTTP server (skip stdio MCP) for shell debugging.

## Things that bite (and how this release handles them)

**The menu used to gate the transport.** Serotonin only pumps `http.Get` / `http.Post` completion callbacks while the menu renders, so the old HTTP bridge silently stalled with the menu closed (commands queued, nothing came back). v0.3.0 moves the transport to plain files read/written synchronously on `onUpdate`, so it no longer depends on the menu at all.

**Memory types.** The verified accepted types are: `byte`, `short`, `ushort`, `int`, `uint`, `int64`, `uint64`, `float`, `double`, `bool`, `string`, `ptr`, `pointer`. Tested working against `memory.GetBase()`. The old `int8/16/32` shortcuts are not accepted.

**`_G` is a native crasher.** Not just nil - even `type(_G)` inside `pcall` takes down the DLL. Blacklisted as regex `\b_G\b`. Use `getfenv(1)` if you need the env table.

**`game.GetService` uses dot syntax, not colon.** `game.GetService("Players")` works; `game:GetService(...)` errors. The Lua `game` is a sandbox proxy table, not an Instance userdata - it also has no `GetChildren`, so tree walks scan via `GetService(...)`.

**Entity API returns userdata, not indices.** Docs say `entity.GetPlayers()` returns integers; it returns userdata objects. Access fields as `p.Name`, `p.Health`; call bone methods as `p:GetBonePosition("HumanoidRootPart")`.

**`entity.Position` is often stale.** In FFA / Tank-style modes the cached position stays at `(0,0,0)`. Use `p:GetBonePosition("HumanoidRootPart")` for the live value. `serotonin_players_full` does this for you.

**Documented-but-broken.** `Vector3:FuzzyEq` doesn't exist (Lua error). `Color3:ToHSV()` crashes (native). `game.GetFFlag` / `game.SetFFlag` crash. `game.LocalPlayer.Backpack` and ~27 other undocumented `LocalPlayer.*` fields crash. All blacklisted.

**`audio.PlaySound` crashes on any non-WAV string,** `cheat.LoadString` raises `"C++ exception"` on every call - both blacklisted. Use `loadstring` / `load` instead of `cheat.LoadString`.

**Don't parallelize eval.** Two simultaneous evals crash Serotonin. The server holds a semaphore and the bridge keeps a single command slot, so calls are serial end-to-end; stay on the tools and you're safe.

## Configuration

Env vars:

- `SEROTONIN_HTTP_HOST` (default `127.0.0.1`)
- `SEROTONIN_HTTP_PORT` (default `8765`)
- `SEROTONIN_HTTP_ONLY=1` - start only the HTTP server, skip stdio MCP. Useful for `curl` debugging.

IPC files (created automatically under the Serotonin sandbox):

- `C:\Serotonin\files\agent\cmd.json` - server -> bridge (one command, written atomically)
- `C:\Serotonin\files\agent\result.json` - bridge -> server (one result)

Tunables in `bridge.lua` (top of file, `CFG` table):

- `op_budget_max` - max native property reads per op before a walk aborts with partial data (default 15000)
- `op_time_max_ms` - soft per-op deadline (default 8000)
- `max_depth` - default serialization depth (default 3)
- `debug` - print per-command lines to the Cheat console

## Credits

Crash diagnosis and the file-based IPC approach: **[mixercodes](https://github.com/mixercodes)**. His [mcp-serotonin-v2](https://github.com/mixercodes/mcp-serotonin-v2) found the holes and pinned the real root cause - the menu-closed / async HTTP-callback stall - and the file-based transport this v0.3.0 release ships is his approach. The crash fixes here are effectively his findings and implementation ported back into this bridge. Thanks for the help. 🙏

## License

MIT.
