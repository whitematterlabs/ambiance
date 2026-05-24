from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

import pytest


REPO = Path(__file__).resolve().parents[1]


def load_paibuild():
    loader = importlib.machinery.SourceFileLoader("paibuild_mod", str(REPO / "paibuild"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], Path | None]] = []

    def run(self, cmd, *, cwd=None, env=None) -> None:  # noqa: ANN001
        self.commands.append((list(cmd), cwd))


def test_dry_run_plans_without_commands_or_build_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paibuild = load_paibuild()
    runner = FakeRunner()
    options = paibuild.Options(config="Release", dry_run=True)

    rc = paibuild.Builder(paibuild.Paths(tmp_path), options, runner=runner).run()

    assert rc == 0
    assert runner.commands == []
    assert not (tmp_path / "macos" / "build").exists()
    out = capsys.readouterr().out
    assert "paibuild plan:" in out
    assert "rebuild swift" in out
    assert "rebuild full-runtime" in out


def test_dry_run_skips_when_state_matches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paibuild = load_paibuild()
    paths = paibuild.Paths(tmp_path)
    (paths.web_dist).mkdir(parents=True)
    (paths.web_dist / "index.html").write_text("<div>ok</div>", encoding="utf-8")
    paths.app_python.parent.mkdir(parents=True)
    paths.app_python.write_text("# python\n", encoding="utf-8")

    fp = paibuild.digest_inputs(paths)
    paibuild.save_state(paths, paibuild.Options(config="Release"), fp)

    rc = paibuild.Builder(
        paths,
        paibuild.Options(config="Release", dry_run=True, skip_registry_check=True),
        runner=FakeRunner(),
    ).run()

    assert rc == 0
    out = capsys.readouterr().out
    assert "skip    swift: up to date" in out
    assert "skip    full-runtime: up to date" in out
    assert "skip    python-package: up to date" in out


def test_paibuild_is_not_a_runtime_console_script(tmp_path: Path) -> None:
    with (REPO / "pyproject.toml").open("rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]
    assert "paibuild" not in scripts

    from bin import paifs_init

    root = tmp_path / "root"
    paifs_init.install_bin_shims(tmp_path / "venv", root)

    assert not (root / "usr" / "bin" / "paibuild").exists()
    assert not (root / "sbin" / "paibuild").exists()


def test_injected_wheel_imports_web_server_from_bundled_dist(tmp_path: Path) -> None:
    paibuild = load_paibuild()
    wheel_dir = tmp_path / "wheels"
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>bundled</html>", encoding="utf-8")

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=REPO,
        check=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    paibuild.inject_web_dist(wheel, dist)

    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(unpacked)

    code = (
        "import boot.entry, boot.init; "
        "import usr.libexec.web.pai_web.server as s; "
        "assert s.FRONTEND_DIST.name == 'dist'; "
        "assert (s.FRONTEND_DIST / 'index.html').read_text() == '<html>bundled</html>'"
    )
    env = {**os.environ, "PYTHONPATH": str(unpacked), "PAI_ROOT": str(tmp_path / "pai")}
    subprocess.run([sys.executable, "-c", code], check=True, cwd=tmp_path, env=env)


def _write_pkg(root: Path, typed_name: str) -> None:
    dest = root / typed_name
    dest.mkdir(parents=True)
    name = typed_name.rsplit("/", 1)[-1]
    kind = typed_name.split("/", 1)[0].rstrip("s")
    if kind == "pais":
        kind = "pai"
    dest.joinpath("package.yaml").write_text(
        f"name: {name}\nkind: {kind}\nversion: 0.1.0\n",
        encoding="utf-8",
    )


def _seed_registry(root: Path, *, omit: str | None = None) -> None:
    from bin import paifs_init

    required = (
        [f"prompts/{n}" for n in paifs_init.ROOT_SEED_PROMPTS]
        + [f"drivers/{n}" for n in paifs_init.KERNEL_SEED_DRIVERS]
        + [f"skills/{n}" for n in paifs_init.KERNEL_SEED_SKILLS]
        + [f"bin/{n}" for n in paifs_init.KERNEL_SEED_BINS]
        + [f"pais/{n}" for n in paifs_init.KERNEL_SEED_PAIS]
    )
    for name in required:
        if name != omit:
            _write_pkg(root, name)


def test_registry_check_accepts_complete_local_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paibuild = load_paibuild()
    registry = tmp_path / "registry"
    _seed_registry(registry)
    monkeypatch.setenv("PAIMAN_REGISTRY", str(registry))

    paibuild.validate_registry(paibuild.Paths(REPO))


def test_registry_check_fails_on_missing_seed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paibuild = load_paibuild()
    registry = tmp_path / "registry"
    _seed_registry(registry, omit="bin/remember")
    monkeypatch.setenv("PAIMAN_REGISTRY", str(registry))

    with pytest.raises(SystemExit, match="bin/remember"):
        paibuild.validate_registry(paibuild.Paths(REPO))
