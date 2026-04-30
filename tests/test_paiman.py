from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bin import paiman
from boot import config as C
from boot import paths


@pytest.fixture
def fhs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    (root / "usr" / "lib" / "pais").mkdir(parents=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "PACKAGES_DIR", root / "usr" / "lib" / "pais", raising=True)
    return root


def test_init_creates_loadable_bundle(fhs_root: Path) -> None:
    assert paiman.main(["init", "email-pai"]) == 0
    bundle = fhs_root / "usr" / "lib" / "pais" / "email-pai"
    assert (bundle / "package.yaml").is_file()
    assert (bundle / "prompt.md").is_file()
    data = yaml.safe_load((bundle / "package.yaml").read_text())
    assert data["kind"] == "pai"
    # Existing resolver must accept what we just produced.
    assert C.resolve_package("email-pai")["kind"] == "pai"


def test_init_refuses_existing_bundle(fhs_root: Path) -> None:
    paiman.main(["init", "dup"])
    with pytest.raises(SystemExit, match="already exists"):
        paiman.main(["init", "dup"])


@pytest.mark.parametrize("bad", ["", ".hidden", "foo/bar"])
def test_init_rejects_invalid_names(fhs_root: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        paiman.main(["init", bad])
