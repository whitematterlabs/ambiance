"""Unit tests for boot.debugger."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import debugger


def test_snapshot_round_trip(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world")
    excluded = tmp_path / "venv"
    excluded.mkdir()
    (excluded / "c.txt").write_text("skip me")

    snap = debugger.snapshot([tmp_path], [excluded])
    keys = set(snap.keys())
    assert str(tmp_path / "a.txt") in keys
    assert str(sub / "b.txt") in keys
    assert str(excluded / "c.txt") not in keys


def test_snapshot_detects_modification(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("before")
    pre = debugger.snapshot([tmp_path], [])
    # bump mtime explicitly so we don't depend on filesystem resolution.
    import os
    os.utime(f, (1000.0, 1000.0))
    post = debugger.snapshot([tmp_path], [])
    touched = debugger._diff(pre, post)
    assert str(f) in touched


def test_parse_lgtm() -> None:
    assert debugger._parse_response("LGTM") is None
    assert debugger._parse_response("  LGTM  ") is None


def test_parse_json_rewrites() -> None:
    raw = '{"files": [{"path": "a.py", "content": "x=1\\n"}]}'
    out = debugger._parse_response(raw)
    assert out == [{"path": "a.py", "content": "x=1\n"}]


def test_parse_json_with_fences() -> None:
    raw = '```json\n{"files": [{"path": "a", "content": "b"}]}\n```'
    out = debugger._parse_response(raw)
    assert out == [{"path": "a", "content": "b"}]


def test_parse_malformed_raises() -> None:
    with pytest.raises(ValueError):
        debugger._parse_response("not json and not LGTM")
    with pytest.raises(ValueError):
        debugger._parse_response('{"files": "not a list"}')


def test_apply_writes_in_scope(tmp_path: Path) -> None:
    target = tmp_path / "good.py"
    target.write_text("old\n")
    touched = {str(target.resolve())}
    rewrites = [{"path": "good.py", "content": "new\n"}]
    applied, warnings = debugger._apply(rewrites, touched, tmp_path, max_lines=50)
    assert applied == ["good.py"]
    assert warnings == []
    assert target.read_text() == "new\n"


def test_apply_rejects_out_of_scope(tmp_path: Path) -> None:
    in_scope = tmp_path / "ok.py"
    in_scope.write_text("ok\n")
    out_of_scope = tmp_path / "evil.py"
    out_of_scope.write_text("untouched\n")
    touched = {str(in_scope.resolve())}
    rewrites = [
        {"path": "ok.py", "content": "ok2\n"},
        {"path": "evil.py", "content": "PWNED\n"},
    ]
    applied, warnings = debugger._apply(rewrites, touched, tmp_path, max_lines=50)
    assert applied == ["ok.py"]
    assert any("evil.py" in w for w in warnings)
    assert out_of_scope.read_text() == "untouched\n"


def test_apply_warns_on_large_rewrite(tmp_path: Path) -> None:
    target = tmp_path / "big.py"
    target.write_text("x\n")
    touched = {str(target.resolve())}
    big = "\n".join(f"line{i}" for i in range(200))
    applied, warnings = debugger._apply(
        [{"path": "big.py", "content": big}], touched, tmp_path, max_lines=10
    )
    # Applied anyway with a warning (auditable, not gated).
    assert applied == ["big.py"]
    assert any("large rewrite" in w for w in warnings)
    assert target.read_text() == big


def test_first_user_text_string() -> None:
    history = [
        {"role": "user", "content": "build me a thing"},
        {"role": "assistant", "content": "ok"},
    ]
    assert debugger._first_user_text(history) == "build me a thing"


def test_first_user_text_blocks() -> None:
    history = [
        {"role": "user", "content": [{"type": "text", "text": "do X"}]},
    ]
    assert debugger._first_user_text(history) == "do X"


def test_last_assistant_text() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "first"},
            {"type": "tool_use", "id": "t1", "name": "shell", "input": {}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert debugger._last_assistant_text(history) == "done"


def test_review_no_touched_files(tmp_path: Path, monkeypatch) -> None:
    """If snapshot diff is empty, no reviewer call is made."""
    import asyncio
    called: list = []

    async def fake_call(*a, **kw):
        called.append((a, kw))
        return "LGTM"

    monkeypatch.setattr(debugger, "_call_reviewer", fake_call)

    config = {"watch_paths": ["."], "exclude": [], "provider": "deepseek", "model": "x"}
    pre = debugger.snapshot([tmp_path], [])
    asyncio.run(debugger.review(
        pai_slug="nonexistent-slug",
        pai_root=tmp_path,
        config=config,
        history=[{"role": "user", "content": "x"}],
        pre_snapshot=pre,
    ))
    assert called == []


def test_review_applies_rewrite(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    target = tmp_path / "f.py"
    target.write_text("buggy\n")
    pre = debugger.snapshot([tmp_path], [])
    # Modify so the diff sees it.
    import os
    os.utime(target, (1000.0, 1000.0))

    async def fake_call(provider, model, system, user):
        return '{"files": [{"path": "f.py", "content": "fixed\\n"}]}'

    monkeypatch.setattr(debugger, "_call_reviewer", fake_call)

    config = {"watch_paths": ["."], "exclude": [], "provider": "deepseek", "model": "x"}
    asyncio.run(debugger.review(
        pai_slug="nonexistent-slug",
        pai_root=tmp_path,
        config=config,
        history=[
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ],
        pre_snapshot=pre,
    ))
    assert target.read_text() == "fixed\n"
