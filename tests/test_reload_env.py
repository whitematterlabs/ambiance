"""boot.reload_env — runtime re-read of .env so web-entered keys go live."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import boot


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    pai_root = tmp_path / "pai_root"
    code_root = tmp_path / "code_root"
    pai_root.mkdir()
    code_root.mkdir()
    monkeypatch.setattr(boot, "_pai_root", pai_root)
    monkeypatch.setattr(boot, "_code_root", code_root)
    monkeypatch.delenv("PAI_TEST_RELOAD_KEY", raising=False)
    return pai_root, code_root


def test_reload_overrides_stale_process_env(roots, monkeypatch):
    # The whole point: a key replaced in .env must beat the value the process
    # loaded at boot (override=False left it stale in os.environ).
    pai_root, _ = roots
    (pai_root / ".env").write_text("PAI_TEST_RELOAD_KEY=fresh\n")
    monkeypatch.setenv("PAI_TEST_RELOAD_KEY", "stale")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "fresh"


def test_reload_precedence_matches_boot(roots):
    # Boot precedence: pai_root beats code_root; .env.local beats .env.
    pai_root, code_root = roots
    (code_root / ".env").write_text("PAI_TEST_RELOAD_KEY=code_env\n")
    (code_root / ".env.local").write_text("PAI_TEST_RELOAD_KEY=code_local\n")
    (pai_root / ".env").write_text("PAI_TEST_RELOAD_KEY=pai_env\n")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "pai_env"
    (pai_root / ".env.local").write_text("PAI_TEST_RELOAD_KEY=pai_local\n")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "pai_local"


def test_reload_missing_files_is_noop(roots, monkeypatch):
    monkeypatch.setenv("PAI_TEST_RELOAD_KEY", "kept")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "kept"
