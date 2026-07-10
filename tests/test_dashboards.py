"""Dashboard discovery — the single parse point for PAI-authored dashboards.

`list_dashboards` walks `/var/lib/dashboards/*.html`, pulls each file's embedded
`application/pai-dashboard+json` manifest, and projects a typed row the console
renders as a tab. The load-bearing invariants: a good manifest round-trips its
title/order/channels; a missing or broken manifest degrades to a slug-titled,
channel-less tab (never a vanished dashboard); and a traversal-y slug can't
address a file outside the dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import paths as PA
from usr.libexec.web.pai_web import dashboards as D


@pytest.fixture
def dash_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the dashboards store to a throwaway dir under tmp PAI_ROOT."""
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    root = tmp_path / "var" / "lib" / "dashboards"
    root.mkdir(parents=True)
    return root


def _write(root: Path, slug: str, manifest: str | None, body: str = "<h1>hi</h1>") -> None:
    parts = []
    if manifest is not None:
        parts.append(
            f'<script type="application/pai-dashboard+json">{manifest}</script>'
        )
    parts.append("<!doctype html>" + body)
    (root / f"{slug}.html").write_text("\n".join(parts), encoding="utf-8")


# --- manifest parsing -------------------------------------------------------


def test_parse_manifest_basic():
    html = (
        '<script type="application/pai-dashboard+json">'
        '{"title": "Sales", "order": 10, "channels": ["procs"]}'
        "</script><!doctype html><body></body>"
    )
    assert D.parse_manifest(html) == {
        "title": "Sales",
        "order": 10,
        "channels": ["procs"],
    }


def test_parse_manifest_missing_is_empty():
    assert D.parse_manifest("<!doctype html><h1>no manifest</h1>") == {}


def test_parse_manifest_bad_json_is_empty():
    html = '<script type="application/pai-dashboard+json">{not json}</script>'
    assert D.parse_manifest(html) == {}


def test_parse_manifest_tolerates_attr_order_and_case():
    html = (
        "<SCRIPT foo='bar' TYPE=\"application/pai-dashboard+json\" data-x='1'>"
        '{"title": "X"}</SCRIPT>'
    )
    assert D.parse_manifest(html) == {"title": "X"}


# --- slug validation --------------------------------------------------------


@pytest.mark.parametrize("slug", ["sales", "sales-pulse", "a1", "a.b_c-2"])
def test_valid_slug_accepts(slug):
    assert D.valid_slug(slug)


@pytest.mark.parametrize(
    "slug", ["", "../etc", "a/b", "-lead", ".hidden", "UPPER", "a b"]
)
def test_valid_slug_rejects(slug):
    assert not D.valid_slug(slug)


# --- list_dashboards --------------------------------------------------------


def test_list_missing_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path / "nope", raising=True)
    assert D.list_dashboards() == []


def test_list_empty_dir_is_empty(dash_root):
    assert D.list_dashboards() == []


def test_list_projects_manifest_fields(dash_root):
    _write(dash_root, "sales", '{"title": "Sales Pulse", "order": 5, "channels": ["procs", "drivers"]}')
    (row,) = D.list_dashboards()
    assert row == {
        "slug": "sales",
        "title": "Sales Pulse",
        "order": 5,
        "channels": ["procs", "drivers"],
    }


def test_list_missing_manifest_falls_back_to_slug(dash_root):
    _write(dash_root, "bare", None)
    (row,) = D.list_dashboards()
    assert row["slug"] == "bare"
    assert row["title"] == "bare"  # slug fallback, not a vanished tab
    assert row["order"] == 100
    assert row["channels"] == []


def test_list_bad_manifest_falls_back(dash_root):
    _write(dash_root, "broken", "{oops")
    (row,) = D.list_dashboards()
    assert row["title"] == "broken"
    assert row["channels"] == []


def test_list_blank_title_falls_back_to_slug(dash_root):
    _write(dash_root, "blank", '{"title": "   "}')
    assert D.list_dashboards()[0]["title"] == "blank"


def test_list_drops_non_string_channels(dash_root):
    _write(dash_root, "mixed", '{"channels": ["procs", 3, null, "drivers"]}')
    assert D.list_dashboards()[0]["channels"] == ["procs", "drivers"]


def test_list_sorts_by_order_then_title(dash_root):
    _write(dash_root, "b", '{"title": "Bravo", "order": 10}')
    _write(dash_root, "a", '{"title": "Alpha", "order": 10}')
    _write(dash_root, "c", '{"title": "Charlie", "order": 1}')
    slugs = [r["slug"] for r in D.list_dashboards()]
    assert slugs == ["c", "a", "b"]  # order 1 first, then Alpha < Bravo


def test_list_ignores_non_html(dash_root):
    _write(dash_root, "real", '{"title": "Real"}')
    (dash_root / "notes.txt").write_text("ignored", encoding="utf-8")
    slugs = [r["slug"] for r in D.list_dashboards()]
    assert slugs == ["real"]


# --- read_dashboard ---------------------------------------------------------


def test_read_returns_raw_html(dash_root):
    _write(dash_root, "sales", '{"title": "Sales"}', body="<h1>Sales</h1>")
    html = D.read_dashboard("sales")
    assert html is not None
    assert "<h1>Sales</h1>" in html
    assert "pai-dashboard+json" in html  # manifest stays inline (single file)


def test_read_missing_is_none(dash_root):
    assert D.read_dashboard("ghost") is None


def test_read_invalid_slug_is_none(dash_root):
    # Even if an attacker plants a file, a traversal slug never resolves.
    assert D.read_dashboard("../etc/passwd") is None
    assert D.read_dashboard("a/b") is None
