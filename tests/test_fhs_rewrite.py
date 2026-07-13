"""Existence-guarded FHS path rewriting.

A PAI sees a chroot-like view where `/` maps to PAI_ROOT, so `/usr/...` in a
command should resolve under the root. But the same syntax names real host
paths a PAI reads off the system and echoes back (`/opt/homebrew/bin/node`).
Blindly prefixing PAI_ROOT corrupted those and crash-looped a supervised
service (2026-07-08). The rewrite now prefers whichever of the two paths
actually exists, defaulting to the PAI-view path for not-yet-created files."""

from __future__ import annotations

from pathlib import Path

from boot._shell_common import rewrite_fhs_path, rewrite_fhs_paths


def _mk(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


def test_real_host_path_preserved(tmp_path: Path) -> None:
    """A path that exists on the host but not under root is left alone.

    This is the 2026-07-08 regression: `/opt/homebrew/bin/node` was being
    rewritten to `<root>/opt/homebrew/bin/node`, which does not exist. We use
    `/bin/sh` (present on every POSIX host) as the portable stand-in."""
    root = tmp_path / "pai"
    root.mkdir()  # <root>/bin/sh does not exist; /bin/sh does
    cmd = "/bin/sh -c true"
    out = rewrite_fhs_paths(cmd, str(root))
    assert out == cmd  # untouched
    assert str(root) not in out


def test_pai_view_path_rewritten(tmp_path: Path) -> None:
    """A path that exists under root is rewritten to the PAI-view path."""
    root = tmp_path / "pai"
    _mk(root, "usr/libexec/browse/server.mjs")
    cmd = "node /usr/libexec/browse/server.mjs"
    out = rewrite_fhs_paths(cmd, str(root))
    assert out == f"node {root}/usr/libexec/browse/server.mjs"


def test_nonexistent_defaults_to_pai_view(tmp_path: Path) -> None:
    """A path that exists neither way keeps chroot semantics (new files)."""
    root = tmp_path / "pai"
    root.mkdir()
    cmd = "echo hi > /tmp/brand_new_file"
    out = rewrite_fhs_paths(cmd, str(root))
    assert out == f"echo hi > {root}/tmp/brand_new_file"


def test_pai_view_wins_when_both_exist(tmp_path: Path) -> None:
    """When both the PAI-view and host paths exist, the view wins.

    `/usr/bin/env` exists on the host; we also create it under root. A PAI
    thinking in its own FHS view means its own copy."""
    root = tmp_path / "pai"
    _mk(root, "usr/bin/env")
    out = rewrite_fhs_paths("/usr/bin/env python", str(root))
    assert out == f"{root}/usr/bin/env python"


def test_word_boundary_not_matched(tmp_path: Path) -> None:
    """`/opt` must not match inside `/optional` — it is not an FHS slot."""
    root = tmp_path / "pai"
    root.mkdir()
    out = rewrite_fhs_paths("ls /optional_dir", str(root))
    assert out == "ls /optional_dir"


def test_relative_path_not_matched(tmp_path: Path) -> None:
    """A slot name mid-path (`foo/usr`) is not a leading FHS path."""
    root = tmp_path / "pai"
    _mk(root, "usr/x")
    out = rewrite_fhs_paths("cat foo/usr/x", str(root))
    assert out == "cat foo/usr/x"


def test_colon_separated_paths(tmp_path: Path) -> None:
    """`:` separates paths (PATH-style); each side is considered on its own."""
    root = tmp_path / "pai"
    _mk(root, "usr/bin/x")
    _mk(root, "bin/y")
    out = rewrite_fhs_paths("PATH=/usr/bin:/bin cmd", str(root))
    assert out == f"PATH={root}/usr/bin:{root}/bin cmd"


def test_single_path_pai_view_wins(tmp_path: Path) -> None:
    """rewrite_fhs_path: PAI-view exists → PAI-view path."""
    root = tmp_path / "pai"
    _mk(root, "tmp/spill.log")
    assert rewrite_fhs_path("/tmp/spill.log", str(root)) == f"{root}/tmp/spill.log"


def test_single_path_host_preserved(tmp_path: Path) -> None:
    """rewrite_fhs_path: host-only path left alone (`/bin/sh` stand-in)."""
    root = tmp_path / "pai"
    root.mkdir()
    assert rewrite_fhs_path("/bin/sh", str(root)) == "/bin/sh"


def test_single_path_nonexistent_defaults_to_pai_view(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    root.mkdir()
    assert rewrite_fhs_path("/tmp/new_file", str(root)) == f"{root}/tmp/new_file"


def test_single_path_non_fhs_and_relative_untouched(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    root.mkdir()
    assert rewrite_fhs_path("/Applications/x.app", str(root)) == "/Applications/x.app"
    assert rewrite_fhs_path("workspace/notes.md", str(root)) == "workspace/notes.md"
    assert rewrite_fhs_path("/optional_dir", str(root)) == "/optional_dir"


def test_single_path_with_colon_and_comma(tmp_path: Path) -> None:
    """A bare path containing `:`/`,` is one path — the command-line regex
    would split it at the delimiter; the single-path variant must not."""
    root = tmp_path / "pai"
    _mk(root, "tmp/a:b,c.log")
    assert rewrite_fhs_path("/tmp/a:b,c.log", str(root)) == f"{root}/tmp/a:b,c.log"


def test_quoted_real_path_preserved(tmp_path: Path) -> None:
    """The single-quoted form from the original incident stays intact."""
    root = tmp_path / "pai"
    _mk(root, "usr/libexec/browse/server.mjs")
    cmd = (
        "paicron start --slug d "
        "--run '/opt/homebrew/bin/node /usr/libexec/browse/server.mjs'"
    )
    out = rewrite_fhs_paths(cmd, str(root))
    # host node path untouched; in-root server.mjs rewritten
    assert "/opt/homebrew/bin/node" in out
    assert f"{root}/opt/homebrew" not in out
    assert f"{root}/usr/libexec/browse/server.mjs" in out
