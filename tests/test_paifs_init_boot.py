"""paifs_init wires the v3 boot/sbin/usr/share/doc slots."""
from __future__ import annotations

from pathlib import Path

from bin.paifs_init import lay_out


def test_lay_out_creates_boot_symlink_to_repo_src(tmp_path: Path) -> None:
    lay_out(tmp_path)
    boot = tmp_path / "boot"
    assert boot.is_symlink(), "expected ~/.pai/boot to be a symlink"
    target = boot.resolve()
    assert target.name == "boot" and target.parent.name == "src", (
        f"expected boot -> repo/src/boot, got {target}"
    )
