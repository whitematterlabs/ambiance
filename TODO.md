# PAI roadmap

The v3 FHS migration is complete. Source of truth for layout is
`src/usr/share/doc/FILESYSTEM_v3.md`.

## Done

- Full FHS skeleton at `$PAI_ROOT` (defaults to `~/.pai/`), provisioned by
  `paifs-init` (boot, etc, usr, var, proc, sys, run, sbin, bin, home,
  root, opt, mnt, tmp, dev).
- Kernel decomposed into `src/boot/` with phased init: sanity → clean →
  probe → reconcile → start → entry. `/sbin/init` is the entrypoint.
- Three-location driver split: config in `/etc/drivers/<name>/`, code in
  `/usr/lib/drivers/<name>/`, runtime state in `/sys/drivers/<name>/`.
- `/proc/<pai>/` namespacing for multi-PAI service supervision.
- Tooling: `paiman init` (bundles), `paiadd` / `paidel` (configure
  instances), `paictl` (instance runtime via `active:` flag),
  `paicron` (services, the cron/systemctl analogue).

## Next

1. **`/opt/` bundle stitching.** Make `paiman install <url>` real:
   clone to `/opt/<pkg>/<ver>/`, resolve declared deps, expose drivers
   and skills via `/usr/lib/`. Unlocks shipping bundles via git.
2. `paiman` verbs beyond `init`: `install`, `uninstall`, `upgrade`,
   `list`. Depends on (1).

## Deferred

- Privileged-write enforcement (capability system: kernelPAI as the sole
  writer to `/etc/`, `/usr/`, `/opt/`, `/var/lib/memory/`; workers route
  through `/root/inbox/` elevation).
- Jailing (`/home/<pai>/` and `/root/` as enforced sandboxes).
- Modular kernel composition.
