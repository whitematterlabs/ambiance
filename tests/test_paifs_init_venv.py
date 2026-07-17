"""Regression tests for paifs_init's FHS venv provisioning."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bin import paifs_init


def test_ensure_venv_uses_current_interpreter_when_creating(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "pai"
    source_python = tmp_path / "clone" / ".venv" / "bin" / "python"
    venv_dir = root / "usr" / "lib" / "venv"
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(cmd)
        if cmd[:2] == ["uv", "venv"]:
            (venv_dir / "bin").mkdir(parents=True)
            (venv_dir / "bin" / "python").write_text("# fhs python\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(paifs_init.sys, "executable", str(source_python))
    monkeypatch.setattr(
        paifs_init,
        "_load_pyproject",
        lambda: {"project": {"dependencies": []}},
    )
    monkeypatch.setattr(paifs_init.subprocess, "run", fake_run)

    assert paifs_init.ensure_venv(root) == venv_dir
    assert calls == [
        ["uv", "venv", "--python", str(source_python), str(venv_dir)]
    ]


def test_ensure_venv_rebuilds_existing_wrong_python(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "pai"
    venv_dir = root / "usr" / "lib" / "venv"
    py = venv_dir / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("# wrong python\n")
    stale = venv_dir / "stale-package"
    stale.write_text("from old interpreter\n")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN202
        if cmd[0] == str(py):
            return SimpleNamespace(stdout="2.7\n")
        calls.append(cmd)
        if cmd[:2] == ["uv", "venv"]:
            (venv_dir / "bin").mkdir(parents=True)
            py.write_text("# rebuilt python\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(
        paifs_init,
        "_load_pyproject",
        lambda: {"project": {"dependencies": []}},
    )
    monkeypatch.setattr(paifs_init.subprocess, "run", fake_run)

    assert paifs_init.ensure_venv(root) == venv_dir
    assert calls == [
        ["uv", "venv", "--python", paifs_init.sys.executable, str(venv_dir)]
    ]
    assert not stale.exists()
    assert py.read_text() == "# rebuilt python\n"


def test_ensure_venv_sets_pyo3_forward_compat_on_python_314(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "pai"
    py = root / "usr" / "lib" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("# fhs python\n")
    install_envs: list[dict[str, str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
        if cmd[:4] == ["uv", "pip", "install", "--python"]:
            install_envs.append(kwargs["env"])
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(paifs_init.sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(paifs_init, "_python_major_minor", lambda _py: (3, 14))
    monkeypatch.setattr(
        paifs_init,
        "_load_pyproject",
        lambda: {"project": {"dependencies": ["litellm[proxy]>=1.0"]}},
    )
    monkeypatch.setattr(paifs_init.subprocess, "run", fake_run)

    assert paifs_init.ensure_venv(root) == root / "usr" / "lib" / "venv"
    assert install_envs
    assert install_envs[0]["PYO3_USE_ABI3_FORWARD_COMPATIBILITY"] == "1"


def test_install_bin_shims_installs_calendar_tool(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    venv_dir = root / "usr" / "lib" / "venv"
    py = venv_dir / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("# fhs python\n")

    paifs_init.install_bin_shims(venv_dir, root)

    cal = root / "usr" / "bin" / "cal"
    assert cal.is_file()
    assert not (root / "sbin" / "cal").exists()
    assert cal.read_text() == (
        f"#!{py}\n"
        "from bin.cal import main\n"
        "raise SystemExit(main())\n"
    )
    assert cal.stat().st_mode & 0o111
