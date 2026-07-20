# FHS Reject-with-Hint (main-branch interim fix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> At execution start, copy this plan to `docs/superpowers/plans/2026-07-20-fhs-reject-with-hint.md`.

**Goal:** Replace the silent FHS-illusion path rewriting on `main` with reject-with-hint: commands and file-tool paths are never mutated; a path spelled in the deprecated FHS-illusion form (`/home/<slug>/...`, `/etc/config.yaml`, ...) is refused with an error naming the real path, and every hit is logged.

**Architecture:** The two rewriters in `src/boot/_shell_common.py` become a pure *classifier* (`classify_fhs_path`) and a command-scanning *detector* (`find_fhs_spellings`) that reuse the existing `_FHS_SLOTS`/`_FHS_PATTERN` tokenizer. Callers (bash tool, persistent shell tool, file-tool resolver) never mutate input: they either pass it through untouched or reject with a hint via their existing error channels (`ShellResult(stderr, exit_code=-1)` for shells, `FileToolResult(is_error=True)` for file tools). Rejections log to kernel stdout (captured into `kernel.log`) and the per-PAI `proc/<slug>/log.md`.

**Tech Stack:** Python 3.14 (uv), pytest. Kernel-only change (`src/boot/`), no registry/pairegistry counterpart (these modules are not dual-homed bins).

## Global Constraints

- Branch: `main`. The working tree currently holds **uncommitted recipient-allowlist work from another session** (`src/boot/config.py`, `src/boot/recipient_allowlist.py`, `src/usr/libexec/web/*`, `tests/test_recipient_allowlist.py`) and main is ahead-1 unpushed. Do not touch, stage, stash, or commit those files. `git add` only the files this plan names. If HEAD has moved past `bea98c9`-era state, re-verify line numbers before editing.
- Commands/paths are NEVER mutated. The only allowed interventions are: run untouched, or refuse with a hint. (This is the point of the change — no "smart fix-ups".)
- Ties go to the host: if a path resolves equally (or deeper) on the host than under `PAI_ROOT`, the command runs literally. Only reject when the spelling resolves *deeper under PAI_ROOT than on the host*.
- Decision (owner-approved): ship reject mode directly, no log-only phase.
- Known accepted behavior changes: (a) `/tmp/newfile` creates now land in the host `/tmp` (previously redirected under `PAI_ROOT/tmp`); (b) where both host and PAI-view paths exist, host now wins (previously PAI-view won).
- Do not deploy live (`pairelease --publish` / `pai update` / `sbin/reboot`) as part of this plan while the other session's work is uncommitted — deploy ships whatever is committed on main; coordinate with the owner at the end.

## Context

PAIs were historically told `/` maps to `PAI_ROOT` (`~/.pai`). The fake `HOME` half of that illusion was already deprecated (`nudge.py` sets the real home; seed prompts no longer teach FHS spellings), but a tolerance shim survives: `rewrite_fhs_paths` (called at `bash_tool.py:153` and `shell_tool.py:678`) and `rewrite_fhs_path` (called via `_file_common.resolve_tool_path:42`) silently translate illusion spellings on every command and file-tool path. Result: honest outputs, lenient inputs — the fleet never converges on real paths, and the shim itself is the system's most dangerous translation layer (build.69: it corrupted `/opt/homebrew/bin/node` and crash-looped a service ~220x/s for 45 min). The v3fs branch will eventually give PAIs a real `/pai` root; this interim fix removes the silent mutation from `main` now, in a way v3fs later deletes cleanly (the hint machinery just goes away).

Existing pieces to reuse (found in recon):
- `_FHS_SLOTS` + `_FHS_PATTERN` tokenizer — `src/boot/_shell_common.py:15-30` (keep as-is).
- `ShellResult` + `render()` — `_shell_common.py:84-100`; shells surface errors as `[stderr]...[exit -1]` text.
- `FileToolResult(text, is_error=True)` — file tools' error channel (propagates a real `is_error` block to the model).
- `processes.append_log(slug, msg)` → `$PAI_ROOT/proc/<slug>/log.md` (raises `ProcessNotFound`; guard like `debugger.py:336`). Kernel-wide lines are `print("[kernel] ...", flush=True)` (captured into `kernel.log`).
- `PAI_SLUG` is available at every call site via the `env` dict (`bash_tool.py:136`, `shell_tool.py:661`, `_file_common.py:31`).

---

### Task 1: Detection core in `_shell_common.py`

**Files:**
- Modify: `src/boot/_shell_common.py` (replace `rewrite_fhs_paths`:33-59 and `rewrite_fhs_path`:62-81; keep `_FHS_SLOTS`, `_PATH_TAIL`, `_FHS_PATTERN`, `ShellResult`)
- Create: `tests/test_fhs_reject.py`
- Delete: `tests/test_fhs_rewrite.py` (all of its behavior is superseded; regression cases are carried over below)

**Interfaces:**
- Produces: `classify_fhs_path(path: str, root: str) -> str | None` — the real path under `root` if `path` is an illusion spelling, else `None`.
- Produces: `find_fhs_spellings(command: str, root: str) -> list[tuple[str, str]]` — ordered, deduped `(token, real_path)` hits in a command line.
- Produces: `fhs_reject_message(hits: list[tuple[str, str]]) -> str` — the hint text.
- Produces: `log_fhs_reject(slug: str, hits: list[tuple[str, str]]) -> None` — kernel print + guarded per-PAI `append_log`.

- [ ] **Step 1: Write the failing tests** (`tests/test_fhs_reject.py`)
- [ ] **Step 2: Run tests to verify they fail** (ImportError expected)
- [ ] **Step 3: Implement in `_shell_common.py`** — delete both rewriters; add `_match_depth`, `classify_fhs_path`, `find_fhs_spellings`, `fhs_reject_message`, `log_fhs_reject`; update module docstring.
- [ ] **Step 4: Run tests to verify they pass**
- [ ] **Step 5: Delete the superseded test file and commit** (`git rm tests/test_fhs_rewrite.py`; stage only named files)

---

### Task 2: Shell tools reject instead of rewrite

**Files:**
- Modify: `src/boot/bash_tool.py` (import at :20, call site at :153)
- Modify: `src/boot/shell_tool.py` (import at :47, call site at :678)

**Interfaces:**
- Consumes: `find_fhs_spellings`, `fhs_reject_message`, `log_fhs_reject` from Task 1.
- Produces: on hit, `ShellResult(stdout="", stderr="bash tool: " + hint, exit_code=-1)`; otherwise the command reaches bash byte-identical.

- [ ] **Step 1: Edit `bash_tool.py`** — reject on hits, else exec `command` untouched.
- [ ] **Step 2: Edit `shell_tool.py`** — same, via `_exec_via_session(sess, command, ...)`.
- [ ] **Step 3: Verify nothing still references the old names** (`rg -n "rewrite_fhs" src/ tests/` → no matches)
- [ ] **Step 4: Run the full suite**
- [ ] **Step 5: Commit**

---

### Task 3: File tools reject via the shared resolver

**Files:**
- Modify: `src/boot/_file_common.py` (`resolve_tool_path`, lines 28-43)
- Modify: `src/boot/read_tool.py`, `src/boot/write_tool.py`, `src/boot/edit_tool.py`
- Test: `tests/test_fhs_reject.py` (append)

**Interfaces:**
- Consumes: `classify_fhs_path`, `log_fhs_reject` from Task 1.
- Produces: `FhsPathError(ValueError)` raised by `resolve_tool_path` for illusion spellings; each file tool catches it and returns `FileToolResult(str(e), is_error=True)`.

- [ ] **Step 1: Write the failing tests** (resolver reject + pass-through)
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — `FhsPathError`, classify in `resolve_tool_path`, catch in read/write/edit tools.
- [ ] **Step 4: Run tests** (targeted + full suite)
- [ ] **Step 5: Commit**

---

### Task 4: Push + live verification (deploy gated)

- [ ] **Step 1: Full suite one last time**
- [ ] **Step 2: Push main**
- [ ] **Step 3: Deploy — ONLY with owner go-ahead** (`uv run pairelease --publish`, then `pai update` + `sbin/reboot`)
- [ ] **Step 4: Live smoke test (after deploy + reboot)** — `cat /etc/config.yaml` refused with hint; `ls /tmp` literal; `echo test > /home/<slug>/smoke.txt` refused; `rg fhs-reject` in kernel.log shows hits.

## Verification (end-to-end)

- `uv run python -m pytest` green.
- `rg -n "rewrite_fhs" src/ tests/` returns nothing.
- Live smoke test above: illusion spellings refused with correct real-path hints, host paths and creates run byte-identical, hits visible in `kernel.log` and `proc/<slug>/log.md`.
- Follow-up (not this plan): grep `fhs-reject` after a few days of live use and fix offending pairegistry skills; v3fs later deletes `classify_fhs_path`/hint machinery wholesale.
