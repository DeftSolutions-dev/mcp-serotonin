# mcp-serotonin

## What is this
Tired of writing ESP blind, guessing whether `entity.GetParts` works in this mode, or watching the c4eat crash because `game.PlaceID` is apparently cursed?

This is an MCP server that bridges any MCP-capable LLM agent (Claude Code, Cursor, Cline, Continue…) to the Serotonin Lua runtime. The agent **sees the live game** - walks the Workspace tree, reads bones and positions, projects world coords to screen, reads memory - then writes Lua tailored to your mode instead of generic templates.

As of **v0.3.0** the transport is **file-based IPC**, so it keeps working with the Serotonin menu **closed** - no more "open the menu or nothing fires", no more stalls under load.

## Why this instead of Studio / debuggers

```
Roblox Studio     - can't attach to a live public server, only your own place files

Ghidra / x64dbg   - see bytes, not game objects; reverse the whole tree before asking
                    "who's alive?"

C4eatEngine       - scans values, can't walk the instance graph or draw overlays

Script executors  - give you Lua, but you write blind and retry on crash
```
**mcp-serotonin** gives an LLM agent live access to all of that at once - tree, entity snapshot, bones, screen projection, memory, arbitrary Lua. The agent verifies what's in the mode before coding, tests hypotheses with `eval`, and ships scripts that work on the first load.

## Tools overview (21 total)
**Diagnostics** - `ping`
**Execution** - `eval`
**Tree** - `tree`, `inspect`, `search_instances`, `find_by_class`, `descendants_stats`, `get_scripts`, `find_player_model`, `nearest`
**Players** - `list_players`, `players_full`, `list_parts`, `part_details`, `parts_count`, `get_bones`
**Screen** - `screen_info`, `project_to_screen`
**Memory** - `memory_base`, `memory_read`, `memory_write`

Commands run **one at a time on the game's update frame** - stacked evals crash the c4eat, so the bridge serializes end-to-end and executes inside `onUpdate`, never inside an async callback. Heavy tree walks are bounded by a per-op read/time budget and skip known crash-prone subtrees instead of materialising the whole tree at once. Crash-triggering APIs like `game.PlaceID`, `_G` and `GetFFlag` are deliberately off the menu via `crash_blacklist.json`.

## How it works

```
MCP client  <-- stdio -->  server.py  <-- files -->  bridge.lua  <-->  Serotonin  <-->  Roblox
                                       agent/cmd.json
                                       agent/result.json
```
`server.py` writes the next command to `C:\Serotonin\files\agent\cmd.json` (atomically, temp + replace), `bridge.lua` reads it on its `onUpdate` frame, runs it on the game thread, and writes `agent/result.json`. No sockets and no async HTTP callbacks - Serotonin only pumps HTTP callbacks while the menu renders, so the old transport stalled with the menu closed; synchronous file IO on `onUpdate` does not.

## Install (4 steps)
- `pip install -r requirements.txt`
- Drop `bridge.lua` into `C:\Serotonin\scripts\`
- Add the stdio entry to your client's MCP config (template in `.mcp.json.example`)
- Load `bridge.lua` in the Scripting tab -> call `serotonin_ping` -> `"pong"` (the menu can stay closed)

Repo: https://github.com/DeftSolutions-dev/mcp-serotonin

## Credits

Crash diagnosis and the file-based IPC approach: **[mixercodes](https://github.com/mixercodes)**. His [mcp-serotonin-v2](https://github.com/mixercodes/mcp-serotonin-v2) found the holes and pinned the real root cause - the menu-closed / async HTTP-callback stall - and the file-based transport this v0.3.0 release ships is his approach. The crash fixes here are effectively his findings and implementation ported back into this bridge. Thanks for the help. 🙏
