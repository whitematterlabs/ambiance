# PAI Filesystem v4 (Linux)

> **Status: the `linux` branch spec.** Supersedes `FILESYSTEM_v3.md`.
> v3 described a quasi-Linux FHS emulated on a macOS host; v4 is the
> real thing: a Linux box where the OS enforces what v3 could only
> state as convention. Sections of v3 not mentioned here (memory
> layout, communication spool, event vocabulary, prompt resolution,
> bundle anatomy) carry forward unchanged.

## What changed from v3

1. **The host is Linux; one team = one box.** The runtime roots at
   `/pai/` on a VPS owned by a single team (an IB desk, a fund team),
   or at `/` when packaged as a container image. v3's "no `/pai/`
   nesting" rule dies with the macOS host it served. The box boundary
   is the hard information barrier — separate machine, disk, network
   identity per team. Unix groups handle only the softer intra-team
   walls. macOS is no longer the runtime — it returns later, if at
   all, as an *edge peripheral* (a driver relaying Mac-only surfaces
   like iMessage to the box).
2. **Principals are team members, enforced by Unix — and we do not
   wrap what Linux provides.** Every member is a real Unix user; their
   PAI, its subagents, and its children run as the member's uid.
   Onboarding is `useradd john && systemctl enable --now pai@john` —
   there is no `paiadd`. Deal walls are `groupadd` / `gpasswd`; a
   member leaving a deal is `gpasswd -d`, an audit event a compliance
   officer can read. Membership and lifecycle are Unix-native;
   `/pai/etc/config.yaml` keeps only what Unix has no slot for
   (per-member model, provider, prompt ref, capability policy).
3. **The kernel dissolves.** v3's `/boot/` monolith fused a
   supervisor with the agent runtime. v4 has no resident kernel
   daemon at all. Its responsibilities split three ways:
   - **Supervision-shaped** (spawn, reap, restart backoff, storm
     guard, memory caps, log transport) → **systemd**. `pai@<member>`
     templated units, `Restart=on-failure`, per-member slices with
     `MemoryMax`, journald.
   - **Agent-shaped** (turn loop, tool execution, LLM calls, retries,
     provider fallback, compaction, mid-turn injection, subagent
     spawn/reap, scheduled-task timers, inbox watching) →
     **member-plane agent runtime** at `/pai/usr/lib/agent/`, one
     sealed root-owned copy, instantiated per member as the member's
     uid. None of this ever needed privilege.
   - **The privileged residue** (approvals, audit log, egress
     credentials, fleet view) → **`pai-broker`**, one small daemon
     under its own system uid. See below.
   "The kernel" survives only as a *package*: the agent runtime, the
   unit files, the updater, and the image build.
4. **pai-broker — the surviving privileged code.** An approval gate
   enforced inside the member's own process is convention (the LLM
   has bash as that uid). Enforcement lives where the credentials
   live — the postfix pattern: the member writes a draft to a spool
   it owns; the broker (own uid, group `adm`) picks it up, checks the
   root-owned capability policy, blocks on the console modal in ask
   mode, sends with credentials only it holds, and appends the audit
   line. The member cannot bypass the gate because the member has
   nothing to send *with*. At v4.0, with integrations deferred, the
   broker is mostly dormant: it serves the fleet view and owns the
   audit log; egress arrives with the first integration. fanotify
   permission events (VFS-level gating) stay earmarked as the fancier
   successor, not required for the port.
5. **The runtime is sealed; no self-healing.** Nothing patches itself
   and re-execs (`sbin/reboot`'s self-patch path is removed). Crash
   recovery is `Restart=on-failure`. Updates are `pai update`: a
   sha-gated atomic swap of the release symlink under `/pai/usr/`,
   then `systemctl restart 'pai@*' pai-broker`. Self-scripting — the
   moat — lives entirely in member-plane userspace, jailed by DAC.
   Python remains the language of all userspace permanently (it is
   what LLMs write best; the product depends on that).
6. **No LLM proxy.** litellm is dropped. Agents call providers
   directly via SDK; retries and fallback are agent-runtime library
   code. The `pai-proxy` system user does not exist.
7. **Events ride the Linux kernel, not a router.** Each agent blocks
   on epoll over its own fds — inotify on its inbox spool, its
   console unix socket, a timerfd for its next scheduled task. Wake-up
   is the OS parking and unparking the process; no daemon of ours
   routes events. v3's FSEvents scar tissue (one-watch-per-path) has
   no Linux analogue.
8. **Install is image-based.** A built image lays the FHS down;
   `paifs-init` becomes the image build step; `pai update` is the
   atomic release swap. One team = one image instance = one box.

## Boot map (one member, `john`)

```
systemd (PID 1)
├── [local-fs]            /pai laid down by the image; nothing to provision
├── pai-broker.service    uid=pai-broker  groups=adm,org
│      loads capability policy from /pai/etc/config.yaml
│      opens /pai/run/broker.sock; owns /pai/var/log/audit.log
│      v4.0: dormant but resident (fleet view, audit)
├── caddy.service         uid=caddy
│      serves the static console PWA (one bundle — vite is dev-only)
│      PAM login → web session bound to a Unix account
│      routes  /api/me/*    → session user's agent socket
│              /api/fleet/* → broker.sock (group adm only)
└── pai@john.service      uid=john  slice user-john.slice, MemoryMax=
       Restart=on-failure
       ExecStart: /pai/usr/lib/venv/bin/python -m agent
       boots unprivileged: reads its config.yaml entry, stitches
       base persona + /pai/home/john/prompt/ overlay, opens inotify
       on its inbox spool + /pai/run/john/api.sock, then sleeps
       (tickless — blocked on epoll)
       └── per-turn children, all uid=john: bash session, subagents
           (reaped by the agent; invisible to systemd)
```

What wakes john — three edges, nothing polls:

```
browser ──PAM login──▶ caddy ──/api/me──▶ john/api.sock ──▶ turn
member  ──file write──▶ spool/john/in/ ──inotify──▶ turn
schedule ──timerfd expiry (agent's own) ──▶ turn
```

An approved send (post-integration, the one privileged path):

```
john's agent ──draft──▶ own outbox spool
pai-broker ──inotify──▶ policy check ──ask──▶ console modal ──ok──▶
broker sends (it alone holds the credential) ──▶ audit.log
```

## Top-level tree (delta view)

```
/pai/                          PAI_ROOT on the team box ("/" in container packaging)
├── sbin/                      root-only tools; init (container packaging PID 1)
├── bin/ → usr/bin/            member-callable tools
├── etc/                       root:root; config.yaml — per-member settings + capability policy
├── home/<member>/             the member's PAI home — <member>:<member> 0700
├── proc/, run/, sys/, tmp/    as v3; /pai/run/ also holds broker.sock + per-member api.sock
├── usr/
│   ├── lib/agent/             member-plane agent runtime (the extracted monolith)
│   ├── lib/skills|pais|venv/  as v3
│   └── share/                 as v3
└── var/
    ├── lib/memory/            team shared memory — root:org 2775 (setgid)
    ├── lib/memory/deals/<slug>/   walled: root:deal-<slug> 2770
    ├── lib/instances/<member>/    <member>:<member> 0700 — sacred, private
    └── log|spool|cache/       as v3; audit.log root:adm; spool inboxes group-mediated
```

`/boot/` has no v4 slot: there is no kernel image to hold. `/pai/usr/lib/drivers/`
is empty at v4.0 (integrations deferred) but the slot survives.

## Ownership map (the enforcement table)

| Path | Owner | Mode | Meaning |
|---|---|---|---|
| `/pai/sbin/`, `/pai/etc/` | `root:root` | 0755 / files 0644 | members read, only root writes |
| `/pai/usr/` | `root:root` | 0755 | sealed release tree; updates land via atomic swap, not edits |
| `/pai/home/<member>/` | `<member>:<member>` | 0700 | nobody else's PAI can read it — the compliance sentence |
| `/pai/var/lib/instances/<member>/` | `<member>:<member>` | 0700 | private memory, workspace, session |
| `/pai/var/lib/memory/` | `root:org` | 2775 | team hivemind: all members read/write, setgid keeps group |
| `/pai/var/lib/memory/deals/<slug>/` | `root:deal-<slug>` | 2770 | intra-team wall: group membership = access |
| `/pai/var/spool/communication/` | `root:org` | 2770 | shared comms archive; per-member `in/` dirs |
| `/pai/var/log/` | `root:adm` | 0750 | append-only; broker + console read via `adm` |
| `/pai/run/<member>/api.sock` | `<member>:<member>` | 0700 dir | the member's console API; caddy connects post-PAM |
| `/pai/run/broker.sock` | `pai-broker:adm` | 0660 | fleet view + approvals; `adm` members only |

Notes:
- **uids**: system users (`pai-broker`, `caddy`) in the distro's system
  range; members are ordinary users in group `org`. No PAI-specific
  uid scheme — Unix conventions apply unmodified.
- **`adm`** is the owner/ops surface: a real member account (whoever
  runs the box) joins group `adm`, which unlocks `/api/fleet/*` and
  log reads. No separate owner identity.
- **Subagents** are transient processes under the member's uid; no new
  principals. Shared team PAIs (group-owned) are deferred until a
  design partner asks.
- **Shared-memory writes**: direct group writes for v4.0.
  Librarian-mediated promotion becomes policy later without changing
  the layout.

## Scale model

Sized for a team: 5–30 members, all `pai@<member>` units resident.
An idle agent blocked on epoll costs ~0 CPU and ~120MB RSS; thirty of
them fit under 4GB — a mid-tier VPS. Per-member `MemoryMax` bounds any
one member's blast radius.

The elastic escape hatch, documented but **not built**: systemd
socket/path/timer activation (`pai-inbox@.path`, `pai@.socket`,
`pai-task@.timer`) starts agents on demand and lets them exit after an
idle window. All durable state is already on disk, so idle-exit is
safe by construction. Flip to this only if a box ever needs hundreds
of enrolled members; RAM then scales with *active* members, not
enrolled ones. What is off the table at any scale: multiplexing
members into one shared runtime process — one process cannot be N
uids, and process-per-member *is* the security model.

## The console

One static PWA bundle (built once; vite exists only in dev), served by
caddy. The Cockpit pattern:

- **Login is PAM** — the web session authenticates as a real Unix
  account. No parallel user database.
- **Member views** are served by the member's *own agent process* over
  its unix socket; caddy routes the authenticated session there.
  Cross-member isolation is DAC, not app-level authz.
- **The fleet view** (and, post-integration, pending approvals) is the
  broker's socket, reachable only by `adm` sessions.

## Dropped from v3

- macOS host support, pyobjc, FSEvents, EventKit, iMessage as a local
  driver, ax/cowork.
- The resident kernel daemon, the reconcile loop, `paiadd`/`paidel`
  (Unix-native now), `paictl` (systemctl now), kernel self-patch +
  `sbin/reboot` re-exec.
- litellm and the `pai-proxy` user.
- The TUI remains dead; the web console (PWA) is the only surface.

## Deferred (carried or new)

- **All integrations** — email, calendar, messaging (Gmail/Google
  Calendar twins included). v4.0 is Linux itself: users, permissions,
  agent runtime, memory tree, member-to-member messages, console.
- The broker's egress path goes live with the first integration; the
  design (spool + policy + modal + audit) is fixed now.
- fanotify/FUSE approval-gate at the VFS layer (the differentiator,
  not the port).
- Per-member namespaces/containers — uids+groups are the 90% solution;
  harder isolation layers on without re-architecture.
- Mac edge peripheral (iMessage relay driver).
- Shared team PAIs with group principals.
- Socket-activation elastic mode (spec'd above, unbuilt).
- A compiled (Go) rewrite — rescoped: with no resident root daemon,
  the only candidate is the broker, and only if a security review
  demands it. Python userspace is permanent.

## Migration reality (empirical, 2026-07-20)

Measured on Ubuntu noble/arm64 (OrbStack VM `pai-linux`): `uv sync`
clean once pyobjc deps went darwin-only; `paifs-init` provisions the
full FHS zero-errors; **the whole test suite passes (900/900)**; the
kernel boots and reconciles the fleet. The port is not a rewrite. The
work, in order: extract the member-plane agent runtime from `/boot/`
and delete the supervisor; systemd units (`pai@`, broker skeleton,
caddy); principal model (useradd + ownership map + PAM console
login); image build. Sequencing lives in the migration plan, not this
spec.
