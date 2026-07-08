"""Driver enable/disable via /proc/<slug>/spec.yaml `active:` flag.

The kernel registry (DRIVER_SPECS) is the source of truth for what drivers
exist; /proc holds the runtime active flag. paictl flips it; reconcile
(triggered by kernel:reload_config events, never on a timer) handles
spawn/cancel."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest
import yaml

from boot import main as M
from boot import processes as P


@pytest.fixture
def proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc = tmp_path / "proc"
    events = tmp_path / "events"
    home = tmp_path / "home"
    proc.mkdir()
    events.mkdir()
    home.mkdir()
    monkeypatch.setattr(P, "PROC_DIR", proc, raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", events, raising=True)
    monkeypatch.setattr(P, "HOME_DIR", home, raising=True)
    return proc


def _spec(proc_root: Path, slug: str) -> dict:
    with (proc_root / slug / "spec.yaml").open() as f:
        return yaml.safe_load(f) or {}


def _write_active(proc_root: Path, slug: str, value: bool) -> None:
    p = proc_root / slug / "spec.yaml"
    spec = _spec(proc_root, slug)
    spec["active"] = value
    with p.open("w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)


def test_driver_active_default_true(proc_root: Path) -> None:
    """First spawn writes active: true into the spec."""
    M._ensure_driver_proc("imessage-in")
    spec = _spec(proc_root, "imessage-in")
    assert spec["kind"] == "driver"
    assert spec["active"] is True


def test_driver_active_preserved_on_restart(proc_root: Path) -> None:
    """A re-spawn (kernel restart) must NOT clobber a paictl-flipped active=false."""
    M._ensure_driver_proc("imessage-in")
    _write_active(proc_root, "imessage-in", False)
    M._ensure_driver_proc("imessage-in")  # simulate kernel restart
    assert _spec(proc_root, "imessage-in")["active"] is False


def test_driver_active_helper(proc_root: Path) -> None:
    """`_driver_active` reads from /proc; missing proc → True."""
    assert M._driver_active("nonexistent") is True
    M._ensure_driver_proc("imessage-in")
    assert M._driver_active("imessage-in") is True
    _write_active(proc_root, "imessage-in", False)
    assert M._driver_active("imessage-in") is False


def test_driver_default_active_false(proc_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest `default_active: false` driver (e.g. the voice host-mic
    listener) must NOT start on reconcile — but its /proc entry is still
    materialized (stopped) so paictl/the web toggle have a flag to flip."""
    started: list[str] = []

    def factory():
        async def coro() -> None:
            started.append("voice-in")
            await asyncio.Event().wait()
        return coro()

    monkeypatch.setattr(
        M,
        "_discover_driver_specs",
        lambda: (("voice-in", factory),),
        raising=True,
    )
    monkeypatch.setitem(M._driver_default_active, "voice-in", False)
    M._driver_tasks.clear()

    async def scenario() -> None:
        # Fresh install + boot: nothing runs, but the proc entry exists,
        # stopped, with active: false.
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        assert "voice-in" not in M._driver_tasks
        assert started == []
        spec = _spec(proc_root, "voice-in")
        assert spec["kind"] == "driver"
        assert spec["active"] is False
        assert P.read_status("voice-in") == "stopped"

        # Owner turns it on (paictl start / web toggle): it runs.
        _write_active(proc_root, "voice-in", True)
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        assert "voice-in" in M._driver_tasks
        assert started == ["voice-in"]

        # Kernel restart with the flag still on: stays on (owner intent wins
        # over the manifest default once the flag exists).
        assert M._driver_active("voice-in") is True

        # Cleanup.
        for t in M._driver_tasks.values():
            t.cancel()
        await asyncio.gather(*M._driver_tasks.values(), return_exceptions=True)
        M._driver_tasks.clear()

    asyncio.run(scenario())


def test_driver_default_active_parses_from_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`default_active: false` in a driver events.yaml lands in the defaults
    map; drivers without the key stay default-on."""
    drivers = tmp_path / "drivers"
    (drivers / "voice").mkdir(parents=True)
    (drivers / "voice" / "events.yaml").write_text(
        "driver: voice\n"
        "processes:\n"
        "  - slug: voice-in\n"
        "    module: drivers.voice.inbound\n"
        "    default_active: false\n"
    )
    (drivers / "imessage").mkdir()
    (drivers / "imessage" / "events.yaml").write_text(
        "driver: imessage\n"
        "processes:\n"
        "  - slug: imessage-in\n"
        "    module: drivers.imessage.inbound\n"
    )
    monkeypatch.setattr(M.paths, "usr_lib_drivers", lambda: drivers, raising=True)
    M._discover_driver_specs()
    assert M._driver_default_active["voice-in"] is False
    assert M._driver_default_active["imessage-in"] is True


def test_reconcile_spawns_and_cancels(
    proc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile picks up active=false and cancels the running task; flipping
    back to true respawns it. Tests the event-driven control plane."""
    started: list[str] = []
    cancelled: list[str] = []

    async def fake_run(slug: str) -> None:
        started.append(slug)
        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            cancelled.append(slug)
            raise

    # _reconcile_drivers re-discovers from /usr/lib/drivers/ on every call,
    # so monkeypatch the discovery function rather than the static tuple.
    monkeypatch.setattr(
        M,
        "_discover_driver_specs",
        lambda: (("imessage-in", lambda: fake_run("imessage-in")),),
        raising=True,
    )
    M._driver_tasks.clear()

    async def scenario() -> None:
        # Boot: active defaults to true → spawned.
        await M._reconcile_drivers()
        await asyncio.sleep(0)  # let the spawned task get a tick
        assert "imessage-in" in M._driver_tasks
        assert started == ["imessage-in"]

        # paictl stop equivalent: flip active, reload.
        _write_active(proc_root, "imessage-in", False)
        await M._reconcile_drivers()
        assert "imessage-in" not in M._driver_tasks
        assert cancelled == ["imessage-in"]

        # paictl start equivalent: flip back, reload.
        _write_active(proc_root, "imessage-in", True)
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        assert "imessage-in" in M._driver_tasks
        assert started == ["imessage-in", "imessage-in"]

        # Cleanup.
        for t in M._driver_tasks.values():
            t.cancel()
        await asyncio.gather(*M._driver_tasks.values(), return_exceptions=True)
        M._driver_tasks.clear()

    asyncio.run(scenario())


def test_reconcile_picks_up_installed_and_removed_driver(
    proc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """paiman install → kernel:reload_config → the new driver's task starts
    without a kernel re-exec; paiman remove → reload → the task stops and
    its registration is dropped."""
    current: list[tuple[str, object]] = []
    monkeypatch.setattr(
        M, "_discover_driver_specs", lambda: tuple(current), raising=True
    )
    M._driver_tasks.clear()

    started: list[str] = []
    cancelled: list[str] = []

    def factory():
        async def coro() -> None:
            started.append("newdrv-in")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append("newdrv-in")
                raise
        return coro()

    async def scenario() -> None:
        # Before install: reload reconciles to an empty set.
        await M._reconcile_drivers()
        assert M._driver_tasks == {}

        # paiman install drops the manifest, then emits reload_config.
        current.append(("newdrv-in", factory))
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        assert "newdrv-in" in M._driver_tasks
        assert started == ["newdrv-in"]
        assert P.read_status("newdrv-in") == "running"

        # Unchanged manifest → no churn on a back-to-back reload.
        task = M._driver_tasks["newdrv-in"]
        await M._reconcile_drivers()
        assert M._driver_tasks["newdrv-in"] is task
        assert started == ["newdrv-in"]

        # paiman remove deletes the manifest, then emits reload_config.
        current.clear()
        await M._reconcile_drivers()
        assert "newdrv-in" not in M._driver_tasks
        assert "newdrv-in" not in M._driver_started_with
        assert cancelled == ["newdrv-in"]

    asyncio.run(scenario())


def test_reconcile_restarts_driver_on_spec_change(
    proc_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reinstall that changes a process's module (or entrypoint) in
    events.yaml restarts the running task with the new spec on the next
    reload; an unchanged manifest leaves the task untouched. Exercises the
    real manifest walk end-to-end."""
    drivers = tmp_path / "drivers"
    (drivers / "foo").mkdir(parents=True)
    manifest = drivers / "foo" / "events.yaml"
    manifest.write_text(
        "driver: foo\nprocesses:\n  - slug: foo-in\n    module: fake_foo_a\n"
    )

    runs: list[str] = []

    def make_mod(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)

        async def run() -> None:
            runs.append(name)
            await asyncio.Event().wait()

        mod.run = run
        return mod

    monkeypatch.setitem(sys.modules, "fake_foo_a", make_mod("fake_foo_a"))
    monkeypatch.setitem(sys.modules, "fake_foo_b", make_mod("fake_foo_b"))
    monkeypatch.setattr(M.paths, "usr_lib_drivers", lambda: drivers, raising=True)
    # Isolate the discovery side-tables so the real walk (which prunes
    # slugs it doesn't find) can't disturb other tests' globals.
    monkeypatch.setattr(M, "_driver_default_active", {}, raising=True)
    monkeypatch.setattr(M, "_driver_spec_identity", {}, raising=True)
    monkeypatch.setattr(M, "_driver_started_with", {}, raising=True)
    M._driver_tasks.clear()

    async def scenario() -> None:
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        assert runs == ["fake_foo_a"]
        t1 = M._driver_tasks["foo-in"]

        # Same manifest → same task object, no restart.
        await M._reconcile_drivers()
        assert M._driver_tasks["foo-in"] is t1
        assert runs == ["fake_foo_a"]

        # Reinstall flips the module → reload restarts with the new spec.
        manifest.write_text(
            "driver: foo\nprocesses:\n  - slug: foo-in\n    module: fake_foo_b\n"
        )
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        t2 = M._driver_tasks["foo-in"]
        assert t2 is not t1
        assert t1.done()
        assert runs == ["fake_foo_a", "fake_foo_b"]
        assert P.read_status("foo-in") == "running"
        assert M._driver_started_with["foo-in"] == ("fake_foo_b", "run")

        # Cleanup.
        for t in M._driver_tasks.values():
            t.cancel()
        await asyncio.gather(*M._driver_tasks.values(), return_exceptions=True)
        M._driver_tasks.clear()

    asyncio.run(scenario())


def test_reconcile_gcs_done_tasks_and_respawns_after_cancelled(
    proc_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: after `_supervise_driver` resolves status as `cancelled`
    (because cancellation was awaited via `_supervise_driver` itself, not
    via `_reconcile_drivers` — e.g. when reload was itself cancelled mid-
    flight), a fresh `paictl start` must respawn the driver. Previously
    the stale done-task in `_driver_tasks` made `running=True` and the
    spawn branch was skipped, and the `cancelled` status on /proc was
    never reset."""

    started: list[str] = []

    def factory():
        async def coro() -> None:
            started.append("imessage-in")
            await asyncio.Event().wait()
        return coro()

    monkeypatch.setattr(
        M,
        "_discover_driver_specs",
        lambda: (("imessage-in", factory),),
        raising=True,
    )
    M._driver_tasks.clear()

    async def scenario() -> None:
        # First reconcile spawns.
        await M._reconcile_drivers()
        await asyncio.sleep(0)
        assert "imessage-in" in M._driver_tasks
        task = M._driver_tasks["imessage-in"]

        # Simulate cancellation that goes through _supervise_driver but
        # leaves a done-task entry in _driver_tasks (e.g. reload was
        # itself cancelled before it could pop the entry).
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        # /proc status is now 'cancelled', _driver_tasks still has the
        # (done) entry.
        assert P.read_status("imessage-in") == "cancelled"
        assert "imessage-in" in M._driver_tasks
        assert M._driver_tasks["imessage-in"].done()

        # paictl start: active=true (it was already true), reload.
        await M._reconcile_drivers()
        await asyncio.sleep(0)

        # Must be a *new* live task, status reset to running.
        assert "imessage-in" in M._driver_tasks
        assert not M._driver_tasks["imessage-in"].done()
        assert P.read_status("imessage-in") == "running"
        assert started == ["imessage-in", "imessage-in"]

        # Cleanup.
        for t in M._driver_tasks.values():
            t.cancel()
        await asyncio.gather(*M._driver_tasks.values(), return_exceptions=True)
        M._driver_tasks.clear()

    asyncio.run(scenario())
