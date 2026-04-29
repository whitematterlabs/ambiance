Staged sequence (smallest → biggest)

  1. Drop /pai/ nesting — rename any */pai/* path segments out (e.g. usr/share/pai/prompts/ → usr/share/prompts/, var/lib/pai/memory/ → var/lib/memory/). Pure rename + import/path-string
  fixups.
  2. Per-PAI home shape — move flat home/ contents under home/<pai>/ (identity.yaml, directives.md, memory/, inbox/, workspace/, tmp/), introduce home/<pai>/memory/{shared →
  /var/lib/memory, private/} split.
  3. Prompt resolution layering — split prompts across usr/share/prompts/ (shipped), etc/prompts/ (override), home/<pai>/prompts/ (per-PAI); add the 3-tier lookup helper.
  4. Driver triad split — move driver code → usr/lib/drivers/<name>/, config (events.yaml) → etc/drivers/<name>/, runtime state → sys/drivers/<name>/.
  5. /proc/<pai>/<svc>/ namespacing — today's run/ (or proc/) gets a <pai>/ level inserted so multi-PAI service supervision works from day one.
  6. bin/+sbin/ skeleton — stub paictl, paimount, paiman entrypoints (paictl first, since service supervision already exists; paimount/paiman are no-ops until /opt/ lands).
  7. Earmarked-but-empty dirs — boot/recovery/, opt/, var/cache/, var/lib/packages/, dev/, run/ (lockfiles sense). Just .gitkeeps + a README per the reserved, not built principle.

  Deferred (don't touch in early commits): privileged-write enforcement, jailing, /opt/ bundle stitching, modular kernel composition.
