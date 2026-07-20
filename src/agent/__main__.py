"""python -m agent — one member's PAI, running as that member's uid.

systemd starts one of these per member (`pai@<member>`); it supervises
nothing but its own turn children. Turn dispatch is not yet ported from
the v3 monolith — until it lands, wakes are logged to stdout (journald)
so the process spine is verifiable end to end.
"""

from __future__ import annotations

import sys

from . import paths
from .loop import WakeLoop


def main() -> int:
    user = paths.member()
    inbox = paths.inbox(user)
    if not inbox.is_dir():
        print(
            f"agent: missing inbox spool {inbox} — box not provisioned"
            f" for member {user!r}",
            file=sys.stderr,
        )
        return 1

    loop = WakeLoop(inbox)
    # Anything delivered while we were down predates the watch.
    for path in sorted(p for p in inbox.iterdir() if p.is_file()):
        print(f"agent[{user}] backlog: {path.name}", flush=True)

    print(f"agent[{user}] up — watching {inbox}", flush=True)
    while True:
        wake = loop.wait()
        if wake is None:
            continue
        for path in wake.inbox:
            print(f"agent[{user}] inbox: {path.name}", flush=True)
        if wake.timer:
            print(f"agent[{user}] timer expired", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
