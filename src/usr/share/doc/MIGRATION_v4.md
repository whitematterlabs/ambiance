# v4 migration — disassembling the monolith

> Companion to FILESYSTEM_v4.md. The kernel (`src/boot/`, 13.3k lines)
> splits three ways: **systemd** (supervision), **`src/agent/`**
> (member-plane runtime, Python), **`src/broker/`** (privileged residue,
> Rust — compiled, single static binary). This file is the disposition
> map: where every `boot/` module goes. `boot/` is deleted when the last
> row is carried.

## Order of work

1. Seed (this map, package/crate/unit skeletons). ✔
2. Agent loop — `src/agent/`, `python -m agent`.
3. systemd units — `pai@.service`, `pai-broker.service`, tmpfiles/sysusers.
4. Rust broker — dormant at v4.0 (socket, policy load, audit append).

## Disposition map

| boot/ module | Fate | Destination / replacement |
|---|---|---|
| `paths.py` | dead | `agent/paths.py` — literal paths, no PAI_ROOT, no PATH rewriting |
| `main.py` (event router) | dead | the Linux kernel: inotify/timerfd/epoll per agent (`agent/loop.py`) |
| `supervisor.py`, `processes.py`, `proctree.py` | dead | systemd: `pai@<member>` units, `Restart=on-failure`, slices, journald |
| `proc_watcher.py`, `driver_health.py`, `doc_watcher.py` | dead | drivers deferred at v4.0; health = `systemctl status` |
| `phases/` (sanity/clean/probe/reconcile/start/backfill/hooks) | dead | image lays the tree down; nothing to reconcile at boot |
| `entry.py`, `init.py` | dead | `python -m agent` under systemd |
| `litellm_proxy.py` | dead | no proxy; direct-SDK providers only |
| `recovery/`, `debugger.py` | dead | runtime is sealed; crash recovery is systemd's |
| `events.py`, `routing.py` | dead | wake_on routing dies; delivery = file into `/var/spool/pai/<member>/in/` |
| `nudge.py` | port | `agent/turn.py` — session jsonl, compaction (soft/hard/overflow), transient retry. Fleet/subagent/overclock/onboarding paths deferred |
| `llm.py` | port | `agent/llm.py` dispatch + `agent/backends/{anthropic,openai}.py` — one module per wire format, no translation layer; history lives in its backend's native shape (wire switch = compact-and-reseed). Dropping litellm un-deferred openrouter (OpenAI-wire native, now direct) |
| `bootstrap.py` | port (lean) | `agent/prompt.py` — base persona + `~/prompt/` overlay + user turn |
| `config.py` | port (lean) | `agent/config.py` — read own member entry; fleet reconcile + capability projection die (systemd / broker) |
| `stitch.py` | dead | the member's real home IS the home; no symlink view. Skills visibility filtering deferred |
| `bash_tool.py`, `shell_tool.py`, `_shell_common.py` | port | `agent/tools/` — real `$HOME`, no FHS illusion, no PATH stitching |
| `read/edit/write/noop_tool.py`, `_file_common.py` | port | `agent/tools/` |
| `bash_gate.py`, `cmd_allowlist.py`, `recipient_allowlist.py` | dead here | enforcement moves to the broker (egress ships with first integration); in-process gating was convention, not enforcement |
| `truncate.py`, `tokens.py`, `image_refs.py` | port | `agent/` — state under `~/.local/state/pai/` |
| `inject.py` | port (rethought) | mid-turn inbox arrivals drain at tool boundaries straight from the spool |
| `timers.py` | port later | scheduled tasks onto the loop's timerfd |
| `claude_backend.py` | port later | claudecode turn executor, post-core |
| `outbound_echo.py`, `skills.py` | port later | with drivers / skills stitching |

## v4.0 message convention

A message to a member is a file dropped in `/var/spool/pai/<member>/in/`
(write elsewhere, `rename(2)` in — the watch fires on `IN_MOVED_TO` /
`IN_CLOSE_WRITE`). Body is the message. **Sender identity is the file's
owner uid** — unforgeable under DAC, no header to trust. The agent
archives consumed messages; replies are just files dropped the other way.
