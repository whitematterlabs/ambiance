# PAI Filesystem v4 (Linux)

> **Status: the `linux` branch spec.** Supersedes `FILESYSTEM_v3.md`.
> v3 described a quasi-Linux FHS emulated on a macOS host; v4 is the
> real thing: a Linux box where the OS enforces what v3 could only
> state as convention. Sections of v3 not mentioned here (memory
> layout, communication spool, event vocabulary, prompt resolution,
> bundle anatomy, the four tools' verbs) carry forward unchanged.

## What changed from v3

1. **The host is Linux.** The runtime roots at `/pai/` on a shared box
   (one org = one box), or at `/` when packaged as a container image.
   v3's "no `/pai/` nesting" rule dies with the macOS host it served.
   macOS is no longer the runtime — it returns later, if at all, as an
   *edge peripheral* (a driver relaying Mac-only surfaces like iMessage
   to the box). EventKit/Quartz/FSEvents code does not port.
2. **Principals are human members, enforced by Unix.** Every org member
   is a real Unix user. The member's PAI, its subagents, and its
   drivers run **as the member's uid**. System PAIs (`root`/kernelPAI,
   `librarian`) are daemon-style system users, like `postfix`. v3's
   "privilege as convention, jailing deferred" becomes DAC: mode bits,
   ownership, groups. Information barriers (per-deal walls) are Unix
   groups managed by `paiadd`.
3. **The monolith splits: root-plane vs member-plane.** v3's `/boot/`
   fused two programs: a supervisor and the agent runtime (LLM loop,
   tools, bootstrap). v4 separates them:
   - **Root-plane** — the kernel proper: reconcile, spawn (setuid to
     the member), reap, route events, nothing else. Runs as root.
     **Imports no userspace code, ever** — v3's `from drivers import…`
     pattern is a defect class here, not a convenience.
   - **Member-plane** — the agent runtime: turn loop, tool execution,
     LLM calls. Userspace code under `/usr/lib/`, spawned per PAI as
     the member's uid. Drivers likewise: always subprocesses under the
     owning uid, never kernel imports.
4. **The kernel is sealed.** kernelPAI no longer patches kernel source
   and re-execs (`sbin/reboot`'s self-patch path is removed). The core
   updates only via the release channel. Self-scripting — the moat —
   lives entirely in member-plane userspace, where it is jailed.
   Phase 2 rewrites the root-plane core (~small, post-split) in Go: a
   static, signed, vendored binary — "the only code that runs as root
   is compiled and sealed." Python remains the language of all
   userspace permanently (it is what LLMs write best; the product
   depends on that).
5. **systemd is PID 1; the kernel is `pai.service`.** The box gets
   journald, watchdog restarts, and cgroups for free. Each member PAI
   runs in its own cgroup slice with `MemoryMax` (ends the
   restart-storm/leak class by fiat). v3's "init *becomes* the kernel"
   survives as the container packaging: in the image build, the pai
   kernel is PID 1 via `/sbin/init` exactly as v3 wrote it.
6. **Events ride inotify.** watchdog's native Linux backend replaces
   FSEvents (and its one-watch-per-path scar tissue). Earmarked, not
   required for the port: approval-gating at the VFS layer via
   fanotify permission events (a blocked `write()` to an outbox that
   waits for owner approval) — the audit log becomes complete by
   construction rather than by driver discipline.
7. **Install is image-based.** v3's open question ("install mechanism:
   repo → /") resolves: a built image lays the FHS down; `paifs-init`
   becomes the image build step; `pai update` is an atomic release
   swap, sha-gated as today. One org = one image instance = one box.

## Top-level tree (delta view)

```
/pai/                          PAI_ROOT on a shared box ("/" in container packaging)
├── boot/                      root-plane kernel only (supervisor; post-split)
├── sbin/                      root-only tools; init (container packaging PID 1)
├── bin/ → usr/bin/            member-callable tools
├── etc/                       root:root; config.yaml declares members + fleet
├── home/<member>/             the member's PAI home — owned <member>:<member> 0700
├── root/                      kernelPAI home (system user)
├── proc/, run/, sys/, tmp/    as v3
├── usr/
│   ├── lib/agent/             member-plane agent runtime (extracted from /boot/)
│   ├── lib/drivers|skills|pais|venv/   as v3
│   └── share/                 as v3
└── var/
    ├── lib/memory/            org shared memory — root:org 2775 (setgid)
    ├── lib/memory/deals/<slug>/   walled: root:deal-<slug> 2770
    ├── lib/instances/<member>/    <member>:<member> 0700 — sacred, private
    └── log|spool|cache/       as v3; spool inboxes group-mediated
```

## Ownership map (the enforcement table)

| Path | Owner | Mode | Meaning |
|---|---|---|---|
| `/pai/boot/`, `/pai/sbin/`, `/pai/etc/` | `root:root` | 0755 / files 0644 | members read, only root writes |
| `/pai/usr/` | `root:root` | 0755 | sealed userspace image; self-authored packages land via root-mediated install, not direct writes |
| `/pai/home/<member>/` | `<member>:<member>` | 0700 | nobody else's PAI can read it — the compliance sentence |
| `/pai/var/lib/instances/<member>/` | `<member>:<member>` | 0700 | private memory, workspace, inbox |
| `/pai/var/lib/memory/` | `root:org` | 2775 | org hivemind: all members read/write, setgid keeps group |
| `/pai/var/lib/memory/deals/<slug>/` | `root:deal-<slug>` | 2770 | information barrier: group membership = wall |
| `/pai/var/spool/communication/` | `root:org` | 2770 | shared comms archive |
| `/pai/var/log/` | `root:adm` | 0750 | append-only; owner console reads |

Notes:
- **uid ranges**: system PAIs 300–399; members 2000+. `paiadd <member>`
  wraps `useradd` (creates the uid, the home stitching, the fleet
  entry) — v3 called paiadd "the useradd analogue"; now it just *is*.
- **Groups**: `org` (everyone), `deal-<slug>` (per-wall, paiadd-managed),
  `adm` (owner console). A member leaving a deal is `gpasswd -d` — an
  audit event a compliance officer can read.
- **Subagents** are transient processes under the member's uid; no new
  principals. Long-lived *shared* team PAIs (owned by a group, not a
  person) are deferred until a design partner asks.
- **Shared-memory write mediation** (v3's open question): direct group
  writes for v4.0. Librarian-mediated promotion becomes policy later
  without changing the layout.

## Process model (delta)

```
systemd (PID 1)
└── pai.service — root-plane kernel (root)
    ├── member PAI proc   uid=alice  cgroup slice, MemoryMax   /proc/<pid>/
    │   ├── drivers…      uid=alice  (subprocesses, never kernel imports)
    │   └── subagents…    uid=alice
    ├── member PAI proc   uid=bob    …
    ├── librarian         uid=librarian (system)
    └── litellm proxy     uid=pai-proxy (system) — the big dep tree, out of root
```

The kernel's job after the split: reconcile `/etc/config.yaml`,
spawn member-plane processes with setuid, reap, route events. Nothing
else. Everything else that `/boot/` does today moves to
`/usr/lib/agent/` and runs unprivileged.

## Dropped from v3

- macOS host support, pyobjc, FSEvents, EventKit calendar (`cal` needs
  a Google Calendar twin), iMessage as a local driver, ax/cowork.
- Kernel self-patch + `sbin/reboot` re-exec of edited source.
- The TUI remains dead; the web console (PWA) is the owner/member surface.

## Deferred (carried or new)

- fanotify/FUSE approval-gate at the VFS layer (the differentiator, not
  the port).
- Per-member namespaces/containers — uids+groups are the 90% solution;
  harder isolation layers on without re-architecture.
- Mac edge peripheral (iMessage relay driver).
- Shared team PAIs with group principals.
- Go root-plane core: **phase 2, immediately post-split** — scoped to
  the extracted supervisor, race-detector-driven, shipped in the first
  design-partner image if timing allows, first security review at the
  latest.

## Migration reality (empirical, 2026-07-20)

Measured on Ubuntu noble/arm64 (OrbStack VM `pai-linux`): `uv sync`
clean once pyobjc deps went darwin-only; `paifs-init` provisions the
full FHS zero-errors; **the whole test suite passes (900/900)**; the
kernel boots and reconciles the fleet. The port is not a rewrite. The
work, in order: monolith split (root-plane/member-plane), principal
model (paiadd→useradd, ownership map), API-stack drivers
(Gmail/Google Calendar), image build, systemd units. Sequencing lives
in the migration plan, not this spec.
