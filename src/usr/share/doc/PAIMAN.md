# PAIMAN — Package manager for PAI

`paiman` is the install/remove tool for everything PAI ships: drivers, skills, prompts, binaries, and PAI bundles. Mutable by design — bundles live in one canonical location on disk and PAI may freely edit them in place to fit its own needs. No content-addressed store, no rollback, no GC. The simplest thing that lets a dependency manifest pull in the right primitives.

## Why mutable, not Nix-style

The Nix model (immutable hashed store + atomic symlink swap) is great when you need rollback, side-by-side versions, and reproducible builds. PAI doesn't. A PAI may want to tweak a skill or prompt to suit its own workflow — that's the whole point of the filesystem-as-data-structure thesis. Immutability fights that. So: one bundle, one location, edit it if you want.

## Bundle types

Five types, each with an obvious slot in the FHS:

| Kind     | Activation slot                  | Form          | Notes |
|----------|----------------------------------|---------------|-------|
| `bin`    | `/usr/bin/<name>`                | file symlink  | symlink → `/opt/paiman/<name>/<entrypoint>`; chmod +x |
| `driver` | `/usr/lib/drivers/<name>/`       | dir symlink   | symlink → `/opt/paiman/<name>/`; kernel reconciles |
| `skill`  | `/usr/lib/skills/<name>/`        | dir symlink   | symlink → `/opt/paiman/<name>/`; contains `SKILL.md` |
| `prompt` | `/usr/share/prompts/<name>.md`   | file symlink  | symlink → `/opt/paiman/<name>/<entrypoint>` |
| `pai`    | `/usr/lib/pais/<name>/`          | dir symlink   | step 2 — pulls in deps from the above |

The activation slots are part of the FHS that root PAI introspects; the canonical install dir under `/opt/paiman/` is opaque to the rest of the system.

## On-disk layout

```
/opt/paiman/<name>/                  # canonical install location for every bundle
    package.yaml                     # bundle manifest
    ...                              # bundle's own files (mutable)
/usr/bin/<name>             -> /opt/paiman/<name>/<entrypoint>      # for bin
/usr/lib/drivers/<name>     -> /opt/paiman/<name>/                  # for driver
/usr/lib/skills/<name>      -> /opt/paiman/<name>/                  # for skill
/usr/share/prompts/<name>.md -> /opt/paiman/<name>/<entrypoint>     # for prompt
/usr/lib/pais/<name>        -> /opt/paiman/<name>/                  # for pai (step 2)
/var/lib/paiman/log.md               # append-only audit log
```

Reinstalling a bundle overwrites its `/opt/paiman/<name>/` directory in place. Activation symlinks are swapped atomically (`rename(2)` on a tmp symlink).

## `package.yaml` schema

Every bundle has one at its root.

```yaml
name: testskill              # required; must match install slot
kind: skill                  # bin | driver | skill | prompt | pai
version: 0.1.0               # informational
entrypoint: SKILL.md         # see per-kind rules below

# pai bundles only — flat list of bundle names. paiman skips any already
# installed; missing ones are resolved from the registry, recursively.
deps:
  - macmail
  - reply-to-email
  - mailsearch
```

`entrypoint` per kind:

- **bin** — required; relative path to the executable. Activation symlinks `/usr/bin/<name>` → `/opt/paiman/<name>/<entrypoint>` and ensures the target is executable.
- **driver** — ignored. The bundle dir itself is what the kernel sees.
- **skill** — defaults to `SKILL.md`; the bundle dir is the activation target. Listed for documentation, not used for activation.
- **prompt** — required; relative path to the `.md` file. Activation symlinks `/usr/share/prompts/<name>.md` → `/opt/paiman/<name>/<entrypoint>`.
- **pai** — ignored; the bundle dir is the activation target.

## Sources

Three install vectors, in order of precedence:

```bash
paiman install testskill1                           # bare name → registry lookup (default)
paiman install ~/dev/email-pai/                     # existing local directory
paiman install github.com/arda/email-pai@v0.3.1     # git URL @ optional ref
```

paiman resolves the argument by trying URL → existing-dir → registry-name. All three end with the bundle at `/opt/paiman/<name>/`.

### Registry

The registry is a flat-layout repo where each top-level directory is one bundle:

```
pairegistry/
    testskill1/
        package.yaml
        SKILL.md
    testbin1/
        package.yaml
        bin/testbin1.py
    ...
```

Configured via `$PAIMAN_REGISTRY` (default `https://github.com/whitematterlabs/pairegistry`). Either a git URL or a local directory of the same shape — local works for tests and offline dev. paiman shallow-clones the registry once per `install` invocation and reuses it for all dep lookups in that run.

For local registry iteration, clone the registry repo and point paiman at the working copy:

```bash
git clone git@github.com:whitematterlabs/pairegistry.git ~/Projects/pairegistry
export PAIMAN_REGISTRY=~/Projects/pairegistry
```

## Commands

```
paiman install <path-or-url>     # ingest a bundle and activate it (overwrites in place)
paiman remove <name>              # remove activation symlink and /opt/paiman/<name>/
paiman list                       # what's installed
paiman show <name>                # print package.yaml
paiman init <name> [--type ...]   # scaffold a new bundle template (legacy pai/subagent for now)
```

## Install flow

1. **Resolve source.** Local dir path → use directly. URL (`http://`, `https://`, `git+`, `github.com/...`) → `git clone --depth 1 <url> <tmpdir>`, optionally checking out a `@<ref>` suffix.
2. **Validate `package.yaml`.** Must exist at source root with `name` and `kind`. Per-kind: `bin`/`prompt` require an `entrypoint` that exists in the source.
3. **Copy to store.** Replace `/opt/paiman/<name>/` with the new tree (excluding `.git/`, `__pycache__/`, `.DS_Store`).
4. **Activate.** Create the activation symlink for the kind, atomically replacing any existing slot via `os.symlink(target, slot+'.tmp'); os.rename(slot+'.tmp', slot)`. Parent dir created if missing.
5. **Log.** Append a one-line entry to `/var/lib/paiman/log.md`.

The kernel reconciles drivers on its own when `events.yaml` changes — paiman doesn't have to wake it up for step 1.

## Remove flow

1. Look up `/opt/paiman/<name>/package.yaml` to learn the kind.
2. Unlink the activation symlink.
3. Delete `/opt/paiman/<name>/`.
4. Append log entry.

For pai bundles (step 2), removing a dependency that another bundle still references is refused — safest default.

## Pai bundles

A pai bundle is just a primitive composed from others. Its `package.yaml` adds a `deps:` list of bare bundle names:

```yaml
name: emailpai
kind: pai
deps:
  - macmail
  - reply-to-email
  - mailsearch
```

When `paiman install` resolves a `kind: pai` source, it walks `deps:` first:

- If `/opt/paiman/<dep>/` already exists, **skip** — installs are mutable and the user may have edited that bundle to fit their needs. paiman doesn't second-guess that.
- Otherwise, look the dep up in the registry and install it (recursively).
- A cycle in deps is an error.

Once all deps are present, the pai bundle itself is copied to `/opt/paiman/<name>/` and the activation symlink at `/usr/lib/pais/<name>/` is laid down. From there, `paiadd` / `paictl` (which sit *above* paiman) configure and run instances.

`paiman remove` refuses to drop a primitive while a pai bundle still lists it in `deps:` — pass `--force` to override.

## Open questions

- **Recursive install for pai deps.** When `paiman install pai/foo` runs and a dep isn't yet installed: refuse, or auto-fetch from a `source:` field on the dep entry? Refuse-and-tell-the-user is simpler; auto-fetch is more useful.
- **Naming conflicts.** Two different bundles want the same `<name>`. Currently last-install wins. May need to refuse if the new bundle's kind differs from the installed one.
- **Edits and reinstall.** A user edits `/opt/paiman/<name>/` then reinstalls from source — their edits are lost (mutable, no diff). May want a `--keep-edits` or warn-on-reinstall later. Punted for now.
- **Git URL trust.** Local paths are trusted by definition. `paiman install github.com/randoperson/x` should at least surface the resolved commit before activation. Punted.
