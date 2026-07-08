"""usr/share/doc wiring: shipped docs link into the release; PAI-authored
built/ docs live durably at var/lib/doc/built and survive release rotation.

Old layout: usr/share/doc was ONE symlink into the repo/release dir, so
`usr/share/doc/built/` physically lived inside `opt/pai/<ver>/` and was
destroyed when `pai update` GC'd old release dirs. The fix (paifs_init
ensure_doc_shipped_links + ensure_built_docs) makes the slot a real dir of
per-file shipped links plus a `built` symlink into /var — and migrates any
real files stranded at the legacy location.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from bin import paifs_init


def _fake_release(tmp_path: Path, name: str, docs: dict[str, str]) -> Path:
    """A stand-in for a repo checkout / opt/pai/<ver> dir: src/usr/share/doc."""
    src = tmp_path / name / "src" / "usr" / "share" / "doc"
    src.mkdir(parents=True)
    for fname, text in docs.items():
        (src / fname).write_text(text)
    return src


def _wire(root: Path, source: Path) -> None:
    """The doc-slot portion of lay_out (dev/tarball mode)."""
    paifs_init.ensure_doc_shipped_links(root, source)
    paifs_init.ensure_built_docs(root)


def test_fresh_root_gets_real_dir_per_file_links_and_durable_built(
    tmp_path: Path,
) -> None:
    source = _fake_release(tmp_path, "rel-a", {"KERNEL.md": "# kernel\n"})
    root = tmp_path / "pai"
    _wire(root, source)

    doc = root / "usr" / "share" / "doc"
    assert doc.is_dir() and not doc.is_symlink()
    kernel = doc / "KERNEL.md"
    assert kernel.is_symlink() and kernel.read_text() == "# kernel\n"

    built = doc / "built"
    durable = root / "var" / "lib" / "doc" / "built"
    assert built.is_symlink()
    assert built.resolve() == durable.resolve()
    # Writes through the view land in /var, not in the release dir.
    (built / "pandoc.md").write_text("# pandoc\n")
    assert (durable / "pandoc.md").read_text() == "# pandoc\n"
    assert not (source / "built").exists()


def test_legacy_whole_dir_symlink_migrates_built_docs_to_var(
    tmp_path: Path,
) -> None:
    source = _fake_release(tmp_path, "rel-a", {"KERNEL.md": "# kernel\n"})
    root = tmp_path / "pai"
    # Legacy layout: usr/share/doc is one symlink into the release dir, and a
    # PAI wrote capability docs "through" it — physically into the release.
    doc = root / "usr" / "share" / "doc"
    doc.parent.mkdir(parents=True)
    doc.symlink_to(source)
    (source / "built").mkdir()
    (source / "built" / "pandoc.md").write_text("# pandoc\n")
    (source / "built" / "deep").mkdir()
    (source / "built" / "deep" / "note.md").write_text("nested\n")

    _wire(root, source)

    durable = root / "var" / "lib" / "doc" / "built"
    assert (durable / "pandoc.md").read_text() == "# pandoc\n"
    assert (durable / "deep" / "note.md").read_text() == "nested\n"
    # The legacy in-release dir is gone; the view now goes through /var.
    assert not (source / "built").exists()
    assert (doc / "built" / "pandoc.md").read_text() == "# pandoc\n"
    assert not doc.is_symlink()


def test_built_docs_survive_release_rotation(tmp_path: Path) -> None:
    rel_a = _fake_release(tmp_path, "rel-a", {"KERNEL.md": "# kernel a\n"})
    root = tmp_path / "pai"
    _wire(root, rel_a)
    doc = root / "usr" / "share" / "doc"
    (doc / "built" / "pandoc.md").write_text("# pandoc\n")

    # `pai update`: new release extracted, paifs-init re-run from it, old
    # release dir wiped (same shape as _gc_versions / rmtree on re-download).
    rel_b = _fake_release(tmp_path, "rel-b", {"KERNEL.md": "# kernel b\n"})
    _wire(root, rel_b)
    shutil.rmtree(tmp_path / "rel-a")

    assert (doc / "KERNEL.md").read_text() == "# kernel b\n"
    assert (doc / "built" / "pandoc.md").read_text() == "# pandoc\n"


def test_dangling_shipped_link_pruned_real_files_kept(tmp_path: Path) -> None:
    rel_a = _fake_release(
        tmp_path, "rel-a", {"KERNEL.md": "# kernel\n", "OLD.md": "# old\n"}
    )
    root = tmp_path / "pai"
    _wire(root, rel_a)
    doc = root / "usr" / "share" / "doc"
    (doc / "NOTES.md").write_text("hand-written\n")  # real file, not a link

    # Next release drops OLD.md; the old release dir is wiped.
    rel_b = _fake_release(tmp_path, "rel-b", {"KERNEL.md": "# kernel b\n"})
    shutil.rmtree(tmp_path / "rel-a")
    _wire(root, rel_b)

    assert not (doc / "OLD.md").is_symlink(), "dangling shipped link not pruned"
    assert (doc / "NOTES.md").read_text() == "hand-written\n"


def test_migration_is_idempotent_and_never_clobbers(tmp_path: Path) -> None:
    source = _fake_release(tmp_path, "rel-a", {"KERNEL.md": "# kernel\n"})
    root = tmp_path / "pai"
    durable = root / "var" / "lib" / "doc" / "built"
    durable.mkdir(parents=True)
    (durable / "pandoc.md").write_text("durable version\n")

    doc = root / "usr" / "share" / "doc"
    doc.parent.mkdir(parents=True)
    doc.symlink_to(source)
    legacy = source / "built"
    legacy.mkdir()
    (legacy / "pandoc.md").write_text("conflicting version\n")  # differs
    (legacy / "jq.md").write_text("# jq\n")  # clean move

    _wire(root, source)
    _wire(root, source)  # idempotent re-run

    # Conflict: both sides preserved, durable copy untouched.
    assert (durable / "pandoc.md").read_text() == "durable version\n"
    assert (legacy / "pandoc.md").read_text() == "conflicting version\n"
    # Clean entry migrated.
    assert (durable / "jq.md").read_text() == "# jq\n"
    assert not (legacy / "jq.md").exists()
    # The conflicted legacy dir blocks the symlink; nothing was lost and the
    # slot is left as the real dir for manual resolution.
    assert (doc / "built").is_dir()


def test_lay_out_skeleton_includes_durable_doc_slot() -> None:
    assert "var/lib/doc/built" in paifs_init.SKELETON


def test_doc_watcher_watches_shipped_and_durable_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recursive usr/share/doc watch stops at the built symlink boundary,
    so the durable /var/lib/doc dir needs (and gets) its own watch."""
    from boot import doc_watcher as dw

    scheduled: list[tuple[str, bool]] = []

    class FakeObserver:
        def schedule(self, handler, path, recursive=False):  # noqa: ANN001
            scheduled.append((path, recursive))

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout=None) -> None:  # noqa: ANN001
            pass

    doc_dir = tmp_path / "usr" / "share" / "doc"
    built_dir = tmp_path / "var" / "lib" / "doc"
    monkeypatch.setattr(dw, "Observer", FakeObserver)
    monkeypatch.setattr(dw, "DOC_DIR", doc_dir)
    monkeypatch.setattr(dw, "BUILT_DOC_DIR", built_dir)

    async def run_briefly() -> None:
        task = asyncio.create_task(dw.run())
        await asyncio.sleep(0)  # let run() schedule its watches and block
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    watched = {p for p, recursive in scheduled if recursive}
    assert str(doc_dir) in watched
    assert str(built_dir) in watched
    assert built_dir.is_dir(), "run() must create the durable dir it watches"
