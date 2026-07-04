# PAIMAN — Package manager for PAI

`paiman` is the install/remove/search tool for everything PAI ships as a
*bundle*: drivers, skills, prompts, bins, libs, and PAI templates. One
canonical location per bundle on disk, one FHS activation symlink per
kind. No content-addressed store, no rollback, no GC. The simplest thing
that lets a dependency manifest pull in the right primitives.

`paiman` sits *below* the configure/run tools. It only lays bundles down
on disk; it does not configure instances or start processes. That work
belongs to `paiadd`/`paidel` (configure an instance from a bundle) and
`paictl`/`paicron` (run it). See `FILESYSTEM_v3.md` for the four-tool
split.

## Mutable by design

Installed bundles live at `/opt/paiman/<name>/` (or
`/opt/paiman/<topic>/<name>/` for topic-foldered kinds like skills). The
copy on disk is **not** treated as immutable — a PAI may edit a skill,
prompt, or driver in place to fit its workflow. That is the whole point
of the filesystem-as-data-structure thesis; immutability would fight it.

Concretely:

- `paiman install <name>` over an existing bundle **overwrites
  `/opt/paiman/<name>/` in place**. Local edits are lost (no diff, no
  `--keep-edits` yet — punted).
- When a `pai` bundle's `deps:` list a primitive that is already
  installed, paiman **skips it**. It will not second-guess a user-edited
  bundle by overwriting it with the registry copy.
- Activation symlinks are swapped atomically
  (`os.symlink(target, slot+'.paiman-tmp'); os.replace(...)`), so the slot
  never points at a half-written tree.

The Nix model (immutable hashed store + atomic swap) buys rollback and
side-by-side versions PAI doesn't need. One bundle, one location, edit
it if you want.

## Bundle kinds

Seven installable kinds, each with one obvious FHS slot:

| Kind     | Activation slot                       | Form         | Notes |
|----------|---------------------------------------|--------------|-------|
| `bin`    | `/usr/bin/<name>`                     | file symlink | symlink → `/opt/paiman/<name>/<entrypoint>`; chmod +x |
| `driver` | `/usr/lib/drivers/<name>/`            | dir symlink  | symlink → `/opt/paiman/<name>/`; kernel reconciles via `events.yaml` |
| `skill`  | `/usr/lib/skills/[<topic>/]<name>/`   | dir symlink  | symlink → `/opt/paiman/[<topic>/]<name>/`; contains `SKILL.md` |
| `prompt` | `/usr/share/prompts/<name>.md`        | file symlink | symlink → `/opt/paiman/<name>/<entrypoint>` |
| `lib`    | `/usr/lib/<name>/`                    | dir symlink  | symlink → `/opt/paiman/<name>/`; importable as `from <name> import …` |
| `pai`    | `/usr/lib/pais/<name>/`               | dir symlink  | composes other bundles via `deps:`; `paiadd` instantiates it |
| `subagent` | `/usr/lib/subagents/<name>/`        | dir symlink  | reusable specialist role; can compose driver/bin deps |

The activation slot is the part the rest of the system introspects;
`/opt/paiman/` is opaque to everything but paiman itself.

## On-disk layout

```
/opt/paiman/<name>/                       # canonical install for every bundle
    package.yaml                          # manifest (kind, entrypoint, deps, hooks)
    ...                                   # bundle's own files (mutable)
/opt/paiman/<topic>/<name>/               # skills with a `topic:` field
/usr/bin/<name>          -> .../<entrypoint>   # bin
/usr/lib/drivers/<name>  -> /opt/paiman/<name>/
/usr/lib/skills/<name>   -> /opt/paiman/<name>/
/usr/lib/<name>          -> /opt/paiman/<name>/   # lib
/usr/share/prompts/<name>.md -> .../<entrypoint>  # prompt
/usr/lib/pais/<name>     -> /opt/paiman/<name>/   # pai template
/usr/lib/subagents/<name> -> /opt/paiman/<name>/  # subagent template
/var/lib/paiman/log.md                    # append-only audit log
```

## `package.yaml`

Every bundle has one at its root. Required fields: `name`, `kind`.

```yaml
name: macmail
kind: driver                 # bin | driver | skill | prompt | lib | pai | subagent
version: 0.1.0               # informational
description: "macOS Mail driver"

# Per-kind:
#   bin    — required `entrypoint:` (relative path to executable)
#   prompt — required `entrypoint:` (relative path to .md file)
#   skill  — optional `topic:` folds the install into /usr/lib/skills/<topic>/
#   driver, pai, subagent — `deps:` list of bundle names or typed refs; resolved recursively
#   lib    — none beyond name/kind

# Optional install-time hooks. Run after activation, with cwd=PAI_ROOT and
# shell=True. Failures are logged but never abort the install (a bad hook
# can't half-uninstall a bundle). Two phases — see "Install hooks vs setup
# hooks" below for when each fires.
hooks:
  install:                       # headless; always runs; 120s cap
    - "bash usr/lib/drivers/<name>/libexec/install.sh"
  setup:                         # interactive; TTY only; 300s cap; skippable
    - "usr/bin/<name>_pair"
```

### Install hooks vs setup hooks

A bundle gets two distinct hook phases, chosen by what the step needs:

| | `hooks.install` | `hooks.setup` |
|---|---|---|
| When | Every install, after activation | Only when stdin **and** stdout are a TTY |
| Prompt | None — runs unattended | `Run interactive setup for '<name>' now? [Y/n]` (default-yes) |
| Timeout | 120s | 300s |
| Headless run (CI, pipe, `paifs-init`) | Runs | **Skipped** — prints `run later: <cmd>` |
| Use for | Idempotent staging the install can always do unattended: copy sidecar sources, `uv pip install` a dep, compile a binary | Steps that need a human, a permission grant, or longer than 120s: OAuth/login, QR pairing, Full-Disk-Access-gated population |

Both run with `cwd=PAI_ROOT`; reference bundle files by their *activated*
path (`usr/lib/drivers/<name>/...`, `usr/bin/<name>`), not the `/opt`
store. A setup hook should exit `0` even when it can't complete (e.g. a
permission not yet granted) and print the one-liner to run later, so the
batch install isn't marked failed — it is expected to be re-runnable and
idempotent.

**Worked example — `email`.** Its one-time history backfill reads the
TCC-protected Mail.app Envelope Index (needs Full Disk Access on the
terminal) and can outlast the 120s install cap, so it is a `setup` hook
(`bash usr/lib/drivers/email/libexec/backfill.sh`), not an `install`
hook. On an interactive install it prompts and runs (migrating a legacy
flat tree first if present, then backfilling — both resumable +
idempotent); on a headless bootstrap it is skipped and the owner runs
`cd ~/.pai && usr/bin/python -m drivers.email.macmail.backfill` after
granting access. `whatsapp` follows the same split: a headless `install`
hook stages the Baileys bridge + deps, an interactive `setup` hook runs
the QR pairing.

`deps:` is a flat list of bare names. paiman walks deps first, fetching
any missing one from the registry. Cycles are a hard error. Already-
installed deps are left alone (mutable contract).

## Sources

Three install vectors, resolved in order URL → existing local dir →
registry name:

```bash
paiman install macmail                            # registry lookup
paiman install ~/dev/macmail/                     # local directory
paiman install github.com/example/macmail@v0.3.1  # git URL @ optional ref
```

URL forms recognized: `http://`, `https://`, `git+...`, `git@...`,
`github.com/...`, `gitlab.com/...`. Local paths must point at a directory
containing `package.yaml`.

### Registry

The registry is a repo where each top-level entry is one bundle, in
either flat or topic-foldered layout. Both are accepted:

```
pairegistry/
    drivers/macmail/package.yaml
    skills/<topic>/<name>/package.yaml
    bins/memorize/package.yaml
    bins/remember/package.yaml
    pais/librarian/package.yaml
    libs/tailer/package.yaml
    prompts/root/package.yaml
```

Configured via `$PAIMAN_REGISTRY` (default
`https://github.com/whitematterlabs/pairegistry`). Either a git URL or a
local directory — local works for tests and offline dev. paiman shallow-
clones the registry once per `install`/`search` invocation and reuses it
for all dep lookups in that run.

For local iteration:

```bash
git clone git@github.com:whitematterlabs/pairegistry.git ~/Projects/pairegistry
export PAIMAN_REGISTRY=~/Projects/pairegistry
```

## Commands

```
paiman install <name | path | url[@ref]>     ingest a bundle and activate it
paiman remove <name> [--force]                remove activation symlink + /opt/paiman/<name>/
paiman list                                   list installed bundles (plus legacy scaffolds)
paiman search [pattern] [--kind ...]         list bundles available in the registry
paiman show <name>                            print package.yaml
paiman init <name> [--type pai|subagent]    scaffold a new bundle (legacy)
```

`remove` refuses to drop a primitive while an installed `pai` bundle
still lists it in `deps:` — pass `--force` to override.

## Standard flow — bringing a new capability online

Four distinct steps. Skipping any of them leaves the capability
unreachable.

```sh
# 1. Discover. What's in the registry?
paiman search                     # everything
paiman search email               # filter by name substring
paiman search --kind pai          # filter by bundle kind

# 2. Install the bundle (resolves and pulls deps recursively).
paiman install email-pai

# 3. Configure an instance of it. Wizard prompts for name, model, etc.;
#    writes /etc/config.yaml + /var/lib/instances/<name>/, emits
#    kernel:reload_config.
paiadd email-pai                  # produces e.g. instance "email"

# 4. Mark the instance active. Flips /proc/<name>/spec.yaml active: true;
#    the supervisor spawns it on the next reconcile.
paictl start email

# 5. (Often required) Re-exec the kernel so new driver wake_on globs
#    are picked up by event routing.
sbin/reboot
```

To discover capabilities for a surface (email, calendar, messages,
contacts), the correct first move is `paiman search <surface>`. The
package manager is the discovery surface; do not grep across kernel
source.

## `paiadd` / `paidel` — instance lifecycle

`paiman` lays down a *template*. `paiadd` turns a template into a
configured *instance*:

```sh
paiadd email-pai                     # interactive wizard
paiadd email-pai --yes --name email \
    --description "owner's email" \
    --provider anthropic --model claude-sonnet-4-6 \
    --wake-on 'gmail:*'              # non-interactive
```

Every PAI declared in `/etc/config.yaml` is, by definition, *persistent*
(long-running, supervised). The reconcile pass writes
`persistent: true` into `/proc/<name>/spec.yaml`. There is no
`--persistent` flag — persistence is implicit in being declared.

`paidel <name>` removes the config entry and detaches the home view.
`paidel <name> --purge` also wipes `/var/lib/instances/<name>/`
(destructive; sacred state goes with it).

### Persubs (persistent subagents)

A `pai` bundle's `package.yaml` may declare `dependencies:` (distinct
from primitive `deps:`) — each entry materializes a *persub*: a long-
lived specialist child of the parent. Persubs get a `/proc/<slug>/`
entry (with `persub: true` and `persistent: true`) but no `/run/pais/`
entry, so they are addressable only by their parent via
`bin/send-message`, not by the kernel router. Reconcile spawns and heals
them. `paiman` only installs the bundle that declares them; spawning is
the kernel's job.

## Install flow (mechanics)

1. **Resolve source.** URL → shallow `git clone` (optional `@ref` →
   `--branch`). Local path → use in place. Bare name → registry lookup.
2. **Validate manifest.** `package.yaml` must exist with `name` and
   `kind`. `bin`/`prompt` require an `entrypoint` that exists in the
   source.
3. **Walk deps.** For `pai`, `driver`, and `subagent` bundles, resolve
   every entry in `deps:` first. Already-installed deps are skipped.
   Cycles error.
4. **Copy to store.** Replace `/opt/paiman/[<topic>/]<name>/` with the
   new tree (excluding `.git`, `__pycache__`, `.DS_Store`, `*.pyc`).
5. **Activate.** Atomically swap the activation symlink for the kind.
   For `bin`, ensure the target has the execute bit.
6. **Run install hooks.** Each `hooks.install` command runs with
   `cwd=PAI_ROOT`, 120s timeout. Failures are logged, never fatal.
6b. **Run setup hooks (TTY only).** If stdin and stdout are both a TTY,
   each `hooks.setup` command is offered (`[Y/n]`, default-yes), then run
   with `cwd=PAI_ROOT`, 300s timeout. A non-interactive install skips this
   phase entirely and prints `run later: <cmd>`. There is **no
   `--no-setup` flag on paiman** — the TTY check is the only gate
   (`--no-reload`, which `paisetup` passes, suppresses only the kernel
   reload in step 8, not hooks).
7. **Audit log.** Append a line to `/var/lib/paiman/log.md`.
8. **Maybe reload.** If any installed bundle is a `skill` or `prompt`,
   emit `kernel:reload_config` so running PAIs re-stitch their homes
   and prompt blocks without a reboot. `driver`/`pai` installs do not
   reload — the user runs `paictl`/`paiadd` next, which reload anyway.
   `bin`/`lib` are picked up via `PATH`/`sys.path` on the next turn.

## Remove flow

1. Look up `/opt/paiman/[<topic>/]<name>/package.yaml` for the kind.
2. Refuse if an installed `pai` bundle has `<name>` in its `deps:`
   (unless `--force`).
3. Unlink the activation symlink for the kind.
4. `rmtree` the bundle dir.
5. Append audit log entry.

## Bootstrap flow — `install.sh` → `paifs-init` → `paisetup`

`paiman` is never called by hand on a fresh machine; `./install.sh` drives
it in two stages, and that is where a bundle's hooks (above) actually fire.

```
./install.sh
  ├─ uv sync ; build web frontend
  ├─ paifs-init --no-setup         # provision $PAI_ROOT, then install the SEED set
  │     └─ paiman install <seed>   # contacts, messages, root/pai_default prompts, …
  ├─ ensure API key
  └─ paisetup                      # interactive registry picker (TTY only)
        └─ paiman install --no-reload <each picked bundle>
              ├─ hooks.install     # runs unattended
              └─ hooks.setup       # prompts [Y/n] — TTY is inherited from paisetup
```

- **Stage 1 — `paifs-init` installs only the seed set** (the constants
  below). It is called with `--no-setup`, which suppresses paifs-init
  *chaining into* `paisetup` (install.sh runs `paisetup` itself next) — it
  does **not** suppress paiman's per-bundle setup hooks. Owner-facing
  drivers like `email` / `imessage` / `macmail` are **not** seeded here.
- **Stage 2 — `paisetup` is the interactive picker** (`src/sbin/paisetup`).
  It runs only when stdin+stdout are a TTY (a piped/CI `install.sh` prints
  "non-interactive shell — skipping" and installs nothing extra). The
  picker surfaces **only drivers** as owner choices; visible drivers are
  auto-checked (`AUTO_CHECKED_KINDS = {"driver"}`) so `email` is selected by
  default. A fixed set (`AUTO_INSTALL_ITEMS` — `browse`, `computer-use`, and
  the `ax`/`calendar`/`imessage`/`notification` drivers) is force-installed
  silently and never rendered. paisetup calls
  `paiman.main(["install", "--no-reload", …])` **in-process**, so the TTY
  propagates and each bundle's `hooks.setup` is offered right there. One
  `kernel:reload_config` is emitted after the whole batch, not per package.

Net effect for a hook author: a `setup` hook fires during an **interactive**
`./install.sh` (once for each picked bundle, default-yes), and is deferred
with a "run later" line on any **headless** install — which is exactly when
a TCC/Full-Disk-Access-gated step like the `email` backfill can't succeed
anyway. See "Install hooks vs setup hooks" above.

### Seed set

On first install of `$PAI_ROOT`, `paifs-init` calls `paiman install` for a
tight seed set declared as module constants in `src/bin/paifs_init.py`:

- `ROOT_SEED_PROMPTS` — `root`, `pai_default`, `subagent`,
  `subagent-persistent`. Stitched into every spawned PAI/subagent
  sysprompt; the kernel will not boot without them.
- `KERNEL_SEED_DRIVERS` — `contacts`, `messages`. Imported as Python
  libraries at module-load time; a missing one raises during boot.
  Drivers with runnable processes (e.g. `imessage`, `macmail`) are NOT
  seeded — the owner installs them explicitly.
- `KERNEL_SEED_SKILLS` — `schedule-reminder`, `grow-capability`,
  `onboard-owner`. Kept tight: only skills that teach the use of a
  kernel-provided tool or first-boot flow the PAI cannot reasonably invent on
  its own. `grow-capability` handles registry discovery/installation only; it
  does not pull a coding-agent dependency.
- `KERNEL_SEED_BINS` — `memorize`, `remember`, `imessage-history`. The
  memory-usage and owner-onboarding boilerplate in the default prompts
  references them directly; without them installed the contract is inert.
- `KERNEL_SEED_PAIS` — `librarian`. Sole writer to shared/private
  MEMORY indexes; reserved fleet member so reconcile spawns it on first
  boot.

Everything else — every app PAI, every owner-facing driver, every
domain skill — is installed later by the owner via `paiman install
<name>`. `paifs-init` is idempotent: re-running it after `git pull`
refreshes shims and the venv, and tops up any seed bundle that has
gone missing.

## Open questions

- **Edits and reinstall.** Reinstalling a bundle silently overwrites
  local edits. May want `--keep-edits` or a warn-on-reinstall. Punted.
- **Naming conflicts.** Two different bundles claiming the same `<name>`
  under different kinds. Currently last-install wins; may need to refuse
  on kind change.
- **Git URL trust.** Local paths are trusted by definition. Random
  `github.com/...` installs should at least surface the resolved commit
  before activation. Punted.
