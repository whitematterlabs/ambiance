# v3fs: real root at /pai

**Status:** draft spec (branch `v3fs`). Amends `FILESYSTEM_v3.md`: the FHS
*layout* is unchanged; what changes is where the root lives and how paths
are spelled. No code yet.

## Problem

`PAI_ROOT` defaults to `~/.pai`, and PAIs are shown a chroot-like illusion
where `/` means `PAI_ROOT`. The illusion is maintained by an in-flight
translation layer:

- `rewrite_fhs_paths` (`src/boot/_shell_common.py:33`), called from
  `bash_tool.py:153` and `shell_tool.py:678`, rewrites command lines.
- `rewrite_fhs_path` (`_shell_common.py:62`) does the same for the file
  tools, one path at a time.
- Both are existence-guarded tri-states, because `/usr/...` in a command
  is ambiguous: it may be the PAI's `/usr` or the host's.

Consequences:

1. **Every path has three spellings**: what the prompt says
   (`/home/john`), what executes (`/Users/arda/.pai/home/john`), and the
   translation between them. The translator is load-bearing and fallible.
   Build.69: a blind rewrite corrupted `/opt/homebrew/bin/node` and
   crash-looped a supervised service ~220x/s for 45 minutes. The guard
   fixed that instance; the ambiguity that caused it is structural.
2. **Policy surfaces operate on translated strings.** The bash allowlist,
   the approval modal, and the audit log see the PAI-view spelling, not
   what actually runs. The owner approves `cat /home/john/notes`, which
   is a string that never executes.
3. **The bash tool is not a literal TTY.** Commands are mutated in
   flight, which is a wrapper in disguise. And the rewrite only covers
   the top-level command line: paths inside a script the PAI wrote a
   minute earlier are whatever it believed at the time.
4. **`HOME=/home/<slug>` is a lie.** `~` expansion, `pwd`, and any
   subprocess that resolves paths disagree with the prompt.

## Decision

Mount a dedicated APFS volume at `/pai` and drop the `/` illusion
entirely. PAI-facing paths are spelled literally (`/pai/home/<slug>`,
`/pai/etc/config.yaml`). Both rewriters are deleted, not guarded better.

One canonical spelling for every path: prompts, shell, allowlist, audit
log, and driver logs all agree, and no PAI path can collide with a host
path.

## Non-goals

- **No chroot.** macOS chroot breaks TCC, keychain, and dyld; there are
  no bind mounts or mount namespaces to fake it.
- **No separate user.** Kernel and drivers must run as the owner (AX/GUI
  session, keychain, chat.db FDA, FSEvents on owner files).
- **No security-boundary change from location alone.** Same uid, same
  perms either way. The write-fence (below) is a follow-on, not this.
- **No `/home` automounter hijack.** Rejected: it only makes homes
  literal, keeps the rewriter alive for `/etc`, `/usr`, `/var`, and adds
  an `/etc/auto_master` maintenance liability.

## Design

### 1. Storage and mount

- APFS volume `pai` in the internal container. Not a partition:
  volumes share the container's free-space pool, so creation is
  instant, allocates nothing, and is reversible with one `diskutil`
  command.
- `/etc/synthetic.conf` entry `pai` creates the empty mount-point dir
  at `/` (SIP-compatible, one reboot).
- Mounted at `/pai` via an `/etc/fstab` UUID entry (`rw`, `nobrowse`
  to keep it out of Finder).
- `diskutil enableOwnership` + volume root `chmod 700`, owned by the
  owner account.
- Nice property for free: when unmounted, `/pai` is an empty dir on
  the sealed read-only snapshot, so writes fail with EROFS instead of
  silently landing on the boot volume.

### 2. Path semantics

- `PAI_ROOT` resolution order: env override (tests, dev roots), then
  `/pai`. v3fs requires the mount; `~/.pai` is what pre-v3fs installs
  run, not a mode of v3fs.
- All PAI-facing paths are literal under `PAI_ROOT`. `HOME` is set to
  the real `/pai/home/<slug>`; `~` works in a bare shell.
- Delete `rewrite_fhs_paths`, `rewrite_fhs_path`, and their call sites
  (`bash_tool.py`, `shell_tool.py`, file tools). The bash tool becomes
  an actual TTY again (restores the literal-TTY dogma).
- Tests are unaffected mechanically: `PAI_ROOT=<tmpdir>` keeps working
  since nothing assumes the illusion anymore.

### 3. Kernel changes

Small and mostly subtractive:

- `_shell_common.py`: delete both rewriters.
- `bash_tool.py:153`, `shell_tool.py:678`, file tools: drop the calls.
- `paths.py`: new default-root resolution (env, `/pai` mountpoint,
  `~/.pai`).
- `bootstrap.py` capability lines and any kernel-emitted text that
  spells `/home/<slug>`: spell `PAI_ROOT`-literal paths.
- Prompt loading substitutes a `{{PAI_ROOT}}` placeholder so registry
  prompts stay generic (no hardcoded `/pai` and no owner tilde). Where
  possible prompts prefer `$HOME`-relative spellings and need no
  placeholder at all.
- `stitch.py`: unchanged mechanics, now emits `/pai`-rooted symlinks.
- `entry.py` chdir-to-root behavior unchanged.

### 4. Registry sweep

Lands on a matching `v3fs` branch in `~/Projects/pairegistry/`, merged
and deployed together with this one.

- Update the PAI-facing-paths rule: "spell literal `PAI_ROOT` paths (or
  `$HOME`-relative); never the FHS-illusion `/home/...`, `/usr/...`".
- Sweep `~/Projects/pairegistry/` prompts and skills for illusion
  spellings (`rg` for `/home/`, `/usr/share`, `/etc/` etc.), replace
  with `{{PAI_ROOT}}` or `$HOME`-relative forms. Seed prompts
  (`root.md`, `pai_default.md`, `capability-escalation.md`) included.

### 5. Provisioning and migration

Beta scope: golden path only, perfect conditions assumed. No fallback
mode, no degraded operation.

`paifs-init` grows an idempotent provision step (invoked by
`install.sh`):

1. Volume exists? Else `diskutil apfs addVolume`.
2. `synthetic.conf` line present? Else append (sudo); reboot once
   before the mount can appear.
3. `fstab` UUID entry present? Else append.
4. `enableOwnership` + `chmod 700`.

Live-machine migration (one-time, owner-run):

1. Stop the kernel. Provision volume, reboot, confirm `/pai` mounted.
2. `rsync -aX ~/.pai/ /pai/` (cross-volume, real copy).
3. Restart kernel, verify `kernel.log` shows root `/pai`.

### 6. Follow-on (explicitly out of scope here)

- **sandbox-exec write-fence** on PAI shells: allow default, deny
  `file-write*` outside `/pai`, `/tmp`, `/var/folders`. Layers under
  the bash approval gate (gate = policy, fence = enforcement). Becomes
  trivial to express once the world is one subpath.
- Read-fence variant, APFS snapshots before `pai update`, volume-level
  `reset`.

## Risks

- **Registry drift**: the sweep must land together with the kernel
  change, or prompts will spell paths the shell no longer translates.
  This is the riskiest coupling in the cutover; paired `v3fs` branches,
  deployed as one release.
- **Backup**: check Time Machine includes the new volume after
  provisioning (one checkbox; the mail-backfill wipe is precedent).
- **claudecode-backend PAIs** bypass `bash_tool` (and the bash gate)
  today, so they never saw the rewriter; literal paths make their
  behavior consistent with everyone else's rather than accidentally
  different.

## Open questions

1. Registry path spelling: since v3fs guarantees the root is `/pai`,
   registry prompts/skills could hardcode `/pai/...` literally (no
   templating at all). The cost is dev/test roots (`PAI_ROOT=<tmpdir>`)
   reading prompts that spell a root they are not running under.
   `{{PAI_ROOT}}` templating at prompt-load keeps those coherent for
   one cheap substitution. Leaning templating.
2. Cutover timing for the live machine: provision `/pai` and migrate
   when the branches merge, or run the v3fs branches live first?
