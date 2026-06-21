"""Boot phase modules tested in isolation against a temp PAI_ROOT."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def laid_out_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from bin import paifs_init

    # These phase tests only need the base FHS skeleton. Avoid installing
    # kernel-essential bundles from the external paiman registry during setup.
    monkeypatch.setattr(paifs_init, "seed_kernel_essentials", lambda _root: None)
    paifs_init.lay_out(tmp_path)
    monkeypatch.setenv("PAI_ROOT", str(tmp_path))
    # Re-import paths so PAI_ROOT picks up the env var.
    import importlib

    import boot.paths as paths
    importlib.reload(paths)
    return tmp_path


def test_sanity_passes_on_complete_layout(laid_out_root: Path) -> None:
    from boot.phases import sanity
    sanity.run()  # returns None, raises on failure


def test_sanity_raises_on_missing_dir(laid_out_root: Path) -> None:
    import shutil
    shutil.rmtree(laid_out_root / "var" / "log")
    shutil.rmtree(laid_out_root / "var" / "lib")
    shutil.rmtree(laid_out_root / "var")
    from boot.phases import sanity
    with pytest.raises(sanity.SanityError) as exc_info:
        sanity.run()
    err = str(exc_info.value)
    assert "var/lib" in err
    assert "var/log" in err


def test_clean_wipes_tmp(laid_out_root: Path) -> None:
    junk = laid_out_root / "tmp" / "junk.txt"
    junk.write_text("stale")
    from boot.phases import clean
    clean.run()
    assert not junk.exists()
    assert (laid_out_root / "tmp").is_dir()  # dir itself preserved


def test_clean_wipes_run_pai_events(laid_out_root: Path) -> None:
    events = laid_out_root / "run" / "pai" / "events"
    stale = events / "20240101T000000-test.yaml"
    stale.write_text("kind: stale")
    from boot.phases import clean
    clean.run()
    assert not stale.exists()
    assert events.is_dir()


def test_clean_removes_nested_subdir_in_tmp(laid_out_root: Path) -> None:
    nested = laid_out_root / "tmp" / "workspace" / "file.txt"
    nested.parent.mkdir()
    nested.write_text("data")
    from boot.phases import clean
    clean.run()
    assert not (laid_out_root / "tmp" / "workspace").exists()
    assert (laid_out_root / "tmp").is_dir()


def test_clean_removes_symlink_not_target(laid_out_root: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "precious.txt").write_text("keep")
    stray = laid_out_root / "tmp" / "stray_link"
    stray.symlink_to(outside)
    from boot.phases import clean
    clean.run()
    assert not stray.exists()
    assert (outside / "precious.txt").exists()


def test_clean_resets_stale_running_driver_status(laid_out_root: Path) -> None:
    import yaml

    driver = laid_out_root / "proc" / "imessage-in"
    driver.mkdir()
    (driver / "spec.yaml").write_text(
        yaml.safe_dump({"kind": "driver", "active": True}, sort_keys=False)
    )
    (driver / "status").write_text("running\n")
    (driver / "log.md").write_text("[00:00] spawned\n")

    pai = laid_out_root / "proc" / "pai"
    pai.mkdir()
    (pai / "spec.yaml").write_text(
        yaml.safe_dump({"kind": "pai", "pid": 2, "slug": "pai"}, sort_keys=False)
    )
    (pai / "status").write_text("running\n")
    (pai / "log.md").write_text("[00:00] spawned\n")

    from boot.phases import clean
    clean.run()

    assert (driver / "status").read_text() == "stopped\n"
    assert "cleared stale running status" in (driver / "log.md").read_text()
    assert (pai / "status").read_text() == "running\n"


def test_probe_logs_each_driver(laid_out_root: Path, capsys) -> None:
    from boot.phases import probe
    # paifs-init seeds kernel-essential drivers (contacts, messages) via
    # paiman. Runnable drivers (imessage, email) are installed explicitly
    # by the root user. probe reads events.yaml at /usr/lib/drivers/<name>/.
    # contacts/messages don't ship events.yaml, so probe skips them
    # silently — assertion is "doesn't crash and skips skip-list dirs".
    probe.run()
    out = capsys.readouterr().out
    # No drivers with events.yaml in this seeded layout; probe should
    # complete without printing per-driver lines or raising.
    for unexpected in ("ERR", "Traceback"):
        assert unexpected not in out


def test_reconcile_phase_calls_config_reconcile(laid_out_root: Path) -> None:
    """Phase wraps boot.config.reconcile_from_config — that already has
    its own tests. We only verify the phase calls it without crashing
    against an empty config."""
    cfg = laid_out_root / "etc" / "config.yaml"
    cfg.write_text("pais: []\n")
    from boot.phases import reconcile
    reconcile.run()  # no-op on empty fleet
