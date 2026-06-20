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
