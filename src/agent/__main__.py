"""python -m agent — one member's PAI, running as that member's uid.

systemd starts one of these per member (`pai@<member>`); it supervises
nothing but its own turn children. The process sleeps in epoll until the
inbox spool or the schedule timer wakes it; each wake drains the spool
and runs one turn. Messages arriving mid-turn are drained at tool
boundaries into the running turn (the v4 form of mid-turn injection).
"""

from __future__ import annotations

import asyncio
import sys

from . import config, messages, paths, prompt
from .loop import WakeLoop
from .turn import Engine


def _drain_factory(inbox):
    def drain() -> list[str]:
        pending = messages.collect(inbox)
        out = []
        for m in pending:
            messages.archive(m)
            out.append(prompt.render_message(m.sender, m.ts, m.body))
        return out

    return drain


async def _turn(engine: Engine, bodies: list[str], drain) -> None:
    await engine.maybe_compact()
    reason = "message" if len(bodies) == 1 else f"{len(bodies)} messages"
    await engine.run(reason, bodies, drain=drain)


async def amain() -> int:
    user = paths.member()
    inbox = paths.inbox(user)
    if not inbox.is_dir():
        print(
            f"agent: missing inbox spool {inbox} — box not provisioned"
            f" for member {user!r}",
            file=sys.stderr,
        )
        return 1
    entry = config.member_entry(user)
    engine = Engine(user, entry)
    loop = WakeLoop(inbox)
    drain = _drain_factory(inbox)

    print(f"agent[{user}] up — watching {inbox}", flush=True)

    # Anything delivered while we were down predates the watch.
    if backlog := drain():
        await _turn(engine, backlog, drain)

    while True:
        wake = await asyncio.to_thread(loop.wait)
        if wake is None:
            continue
        if wake.timer:
            # Scheduled tasks land with the timer port (MIGRATION_v4.md).
            print(f"agent[{user}] timer expired (scheduler not yet ported)", flush=True)
        # A mid-turn drain may have consumed the files behind these events
        # already — collect() deciding "nothing new" is the dedupe.
        if bodies := drain():
            await _turn(engine, bodies, drain)


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
