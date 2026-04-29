"""Boot phase modules tested in isolation against a temp PAI_ROOT."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def laid_out_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from bin.paifs_init import lay_out
    lay_out(tmp_path)
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
    assert "var/log" in str(exc_info.value) or "var" in str(exc_info.value)
