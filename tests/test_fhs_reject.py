"""Reject-with-hint for FHS-illusion paths.

The old rewriters silently translated `/home/<slug>/...`-style spellings
to host paths. They are gone: paths are classified, never mutated.
"""
from pathlib import Path

from boot._shell_common import (
    classify_fhs_path,
    find_fhs_spellings,
    fhs_reject_message,
)


def _mk(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


# --- classify_fhs_path ---

def test_host_path_never_touched(tmp_path: Path) -> None:
    # /bin/sh exists on the host; must classify as literal even if a
    # PAI-view twin exists (host full-path existence always wins).
    root = tmp_path / "pai"
    _mk(root, "bin/sh")
    assert classify_fhs_path("/bin/sh", str(root)) is None


def test_pai_view_only_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    _mk(root, "etc/config.yaml")
    assert (
        classify_fhs_path("/etc/config.yaml", str(root))
        == f"{root}/etc/config.yaml"
    )


def test_create_under_pai_home_is_rejected(tmp_path: Path) -> None:
    # Neither side has the full path, but /home/john resolves deeper
    # under root (root/home/john exists) than on the host (/home only).
    root = tmp_path / "pai"
    (root / "home" / "john").mkdir(parents=True)
    assert (
        classify_fhs_path("/home/john/new/notes.md", str(root))
        == f"{root}/home/john/new/notes.md"
    )


def test_ancestor_tie_goes_to_host(tmp_path: Path) -> None:
    # /tmp exists on the host and under root: equal depth, host wins,
    # command runs literally (create lands in host /tmp).
    root = tmp_path / "pai"
    (root / "tmp").mkdir(parents=True)
    assert classify_fhs_path("/tmp/brand_new_file", str(root)) is None


def test_non_fhs_and_relative_untouched(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    root.mkdir()
    assert classify_fhs_path("/Applications/x.app", str(root)) is None
    assert classify_fhs_path("relative/usr/x", str(root)) is None
    assert classify_fhs_path("/optional_dir", str(root)) is None


# --- find_fhs_spellings ---

def test_command_scan_flags_only_illusion_tokens(tmp_path: Path) -> None:
    # The build.69 incident command: the real host node path must NOT be
    # flagged; the PAI-view-only server.mjs must be.
    root = tmp_path / "pai"
    _mk(root, "usr/libexec/browse/server.mjs")
    cmd = (
        "paicron start --slug d "
        "--run '/opt/homebrew/bin/node /usr/libexec/browse/server.mjs'"
    )
    hits = find_fhs_spellings(cmd, str(root))
    assert hits == [
        ("/usr/libexec/browse/server.mjs", f"{root}/usr/libexec/browse/server.mjs")
    ]


def test_colon_separated_tokens_scanned_independently(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    _mk(root, "usr/bin/mytool")
    hits = find_fhs_spellings("PATH=/usr/bin/mytool:/bin/sh", str(root))
    assert hits == [("/usr/bin/mytool", f"{root}/usr/bin/mytool")]


def test_duplicate_tokens_deduped(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    _mk(root, "etc/config.yaml")
    hits = find_fhs_spellings(
        "cat /etc/config.yaml /etc/config.yaml", str(root)
    )
    assert len(hits) == 1


def test_clean_command_yields_no_hits(tmp_path: Path) -> None:
    root = tmp_path / "pai"
    root.mkdir()
    assert find_fhs_spellings("ls -la && echo hi > out.txt", str(root)) == []


# --- fhs_reject_message ---

def test_message_names_token_and_real_path() -> None:
    msg = fhs_reject_message([("/etc/config.yaml", "/Users/a/.pai/etc/config.yaml")])
    assert "/etc/config.yaml" in msg
    assert "/Users/a/.pai/etc/config.yaml" in msg


# --- resolve_tool_path ---

def test_resolver_rejects_illusion_spelling(tmp_path, monkeypatch) -> None:
    import boot.paths as paths
    from boot._file_common import FhsPathError, resolve_tool_path

    root = tmp_path / "pai"
    _mk(root, "etc/config.yaml")
    monkeypatch.setattr(paths, "PAI_ROOT", root)
    try:
        resolve_tool_path("/etc/config.yaml", {"PAI_SLUG": "john"})
        raise AssertionError("expected FhsPathError")
    except FhsPathError as e:
        assert f"{root}/etc/config.yaml" in str(e)


def test_resolver_passes_host_paths_untouched(tmp_path, monkeypatch) -> None:
    import boot.paths as paths
    from boot._file_common import resolve_tool_path

    root = tmp_path / "pai"
    root.mkdir()
    monkeypatch.setattr(paths, "PAI_ROOT", root)
    assert resolve_tool_path("/bin/sh", {"PAI_SLUG": "john"}) == Path("/bin/sh")
