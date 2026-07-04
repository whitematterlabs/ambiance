"""paifs_init wires the v3 boot/sbin/usr/share/doc slots."""
from __future__ import annotations

from pathlib import Path

import yaml

from bin.paifs_init import default_config_yaml, lay_out


def test_lay_out_creates_boot_symlink_to_repo_src(tmp_path: Path) -> None:
    lay_out(tmp_path)
    boot = tmp_path / "boot"
    assert boot.is_symlink(), "expected ~/.pai/boot to be a symlink"
    target = boot.resolve()
    assert target.name == "boot" and target.parent.name == "src", (
        f"expected boot -> repo/src/boot, got {target}"
    )


def test_seed_config_puts_selected_provider_on_all_seed_pais() -> None:
    cfg = yaml.safe_load(
        default_config_yaml(provider="openai", model="gpt-5.5")
    )
    by_name = {entry["name"]: entry for entry in cfg["pais"]}

    for name in ("root", "pai", "librarian"):
        assert by_name[name]["provider"] == "openai"
        assert by_name[name]["model"] == "gpt-5.5"
