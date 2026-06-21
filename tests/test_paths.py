from __future__ import annotations

import os
from pathlib import Path

from boot import paths


def test_build_pai_path_adds_host_defaults_when_current_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        paths,
        "HOST_SYSTEM_PATH_DIRS",
        ("/usr/bin", "/bin", "/usr/sbin"),
        raising=True,
    )

    parts = paths.build_pai_path("").split(os.pathsep)

    assert parts[:3] == [
        str(tmp_path / "usr" / "lib" / "venv" / "bin"),
        str(tmp_path / "usr" / "bin"),
        str(tmp_path / "sbin"),
    ]
    assert parts[-3:] == ["/usr/bin", "/bin", "/usr/sbin"]


def test_build_pai_path_dedupes_existing_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(paths, "HOST_SYSTEM_PATH_DIRS", ("/bin",), raising=True)

    fhs_bin = str(tmp_path / "usr" / "bin")
    parts = paths.build_pai_path(os.pathsep.join([fhs_bin, "/bin"])).split(os.pathsep)

    assert parts.count(fhs_bin) == 1
    assert parts.count("/bin") == 1


def test_build_pai_path_host_first_keeps_system_before_pai_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(paths, "HOST_SYSTEM_PATH_DIRS", ("/bin", "/usr/sbin"), raising=True)

    parts = paths.build_pai_path("", host_first=True).split(os.pathsep)

    assert parts == [
        str(tmp_path / "usr" / "lib" / "venv" / "bin"),
        "/bin",
        "/usr/sbin",
        str(tmp_path / "usr" / "bin"),
        str(tmp_path / "sbin"),
    ]


def test_host_executable_ignores_shadowing_current_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shadow_dir = tmp_path / "shadow"
    host_dir = tmp_path / "host"
    shadow_dir.mkdir()
    host_dir.mkdir()
    shadow = shadow_dir / "ps"
    host = host_dir / "ps"
    shadow.write_text("#!/bin/sh\nexit 9\n")
    host.write_text("#!/bin/sh\nexit 0\n")
    shadow.chmod(0o755)
    host.chmod(0o755)

    monkeypatch.setenv("PATH", str(shadow_dir))
    monkeypatch.setattr(paths, "HOST_SYSTEM_PATH_DIRS", (str(host_dir),), raising=True)

    assert paths.host_executable("ps") == str(host)
