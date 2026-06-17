"""Tests for src/kernel/config.py — load, validate, reconcile."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boot import config as C
from boot import processes as P


def _write_config(repo_root: Path, body: str) -> Path:
    path = repo_root / "etc" / "config.yaml"
    path.write_text(body)
    return path


def _write_package(repo_root: Path, name: str, body: dict) -> Path:
    pkg_dir = repo_root / "packages" / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    path = pkg_dir / "package.yaml"
    with path.open("w") as f:
        yaml.safe_dump(body, f)
    return path


# ----- load_config / validation -----


def test_load_minimal(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    cfg = C.load_config()
    assert set(cfg) == {"root", "pai"}
    assert cfg["root"]["pid"] == 1
    assert cfg["pai"]["description"] == "dflt"


def test_missing_file(repo_root):
    with pytest.raises(C.ConfigError, match="not found"):
        C.load_config()


def test_duplicate_name(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: a
  - name: root
    pid: 2
    description: b
""",
    )
    with pytest.raises(C.ConfigError, match="duplicate name"):
        C.load_config()


def test_duplicate_pid(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: a
  - name: pai
    pid: 1
    description: b
""",
    )
    with pytest.raises(C.ConfigError, match="reserved for"):
        C.load_config()


def test_reserved_pid_wrong_name(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: not_root
    pid: 1
    description: nope
""",
    )
    with pytest.raises(C.ConfigError, match="reserved for 'root'"):
        C.load_config()


def test_reserved_name_wrong_pid(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 99
    description: oops
""",
    )
    with pytest.raises(C.ConfigError, match="reserved entry"):
        C.load_config()


def test_missing_description(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
""",
    )
    with pytest.raises(C.ConfigError, match="description"):
        C.load_config()


def test_provider_unknown(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    provider: not-a-provider
""",
    )
    with pytest.raises(C.ConfigError, match="unknown provider"):
        C.load_config()


def test_provider_openai_accepted(repo_root, live_dir):
    # Regression guard for the openai row: config must accept `provider: openai`
    # now that it exists in L.PROVIDERS, and persist it to spec.yaml.
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    provider: openai
    model: gpt-5.5
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["provider"] == "openai"
    assert spec["model"] == "gpt-5.5"


def test_provider_persisted(repo_root, live_dir):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    provider: deepseek
    model: deepseek-v4-pro
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["provider"] == "deepseek"
    assert spec["model"] == "deepseek-v4-pro"


def test_wake_on_type(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    wake_on: "not-a-list"
""",
    )
    with pytest.raises(C.ConfigError, match="wake_on"):
        C.load_config()


# ----- package merge -----


def test_package_merge(repo_root):
    _write_package(
        repo_root,
        "msg_spec",
        {
            "kind": "pai",
            "name": "message_specialist",
            "description": "from package",
            "model": "deepseek-v4-pro",
            "prompt": "prompt.md",
            "wake_on": ["imessage:*"],
        },
    )
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: msg
    package: msg_spec
    description: inline-override
""",
    )
    cfg = C.load_config()
    msg = cfg["msg"]
    # inline `description` overrides package
    assert msg["description"] == "inline-override"
    # package fields flow through
    assert msg["model"] == "deepseek-v4-pro"
    assert msg["wake_on"] == ["imessage:*"]


def test_package_kind_unsupported(repo_root):
    _write_package(repo_root, "skill_pkg", {"kind": "skill", "name": "x", "description": "y"})
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: x
    package: skill_pkg
    description: huh
""",
    )
    with pytest.raises(NotImplementedError, match="skill"):
        C.load_config()


def test_reconcile_persub_bundle_preserves_fhs_relative_prompt(
    repo_root, live_dir, monkeypatch
):
    subagents = repo_root / "usr" / "lib" / "subagents"
    pkg_dir = subagents / "computer-use"
    pkg_dir.mkdir(parents=True)
    monkeypatch.setattr(C, "SUBAGENTS_DIR", subagents, raising=True)
    (pkg_dir / "package.yaml").write_text(
        "name: computer-use\n"
        "kind: subagent\n"
        "version: 0.1.0\n"
        "prompt: usr/lib/subagents/computer-use/prompt.md\n"
    )
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
    dependencies:
      - name: computer-use
        description: macOS UI operator
        package: computer-use
""",
    )

    C.reconcile_from_config()

    spec = P.read_spec("pai.computer-use")
    assert spec["package"] == "computer-use"
    assert spec["prompt"] == "usr/lib/subagents/computer-use/prompt.md"


def test_reconcile_existing_persub_heals_package_metadata(
    repo_root, live_dir, monkeypatch
):
    subagents = repo_root / "usr" / "lib" / "subagents"
    pkg_dir = subagents / "computer-use"
    pkg_dir.mkdir(parents=True)
    monkeypatch.setattr(C, "SUBAGENTS_DIR", subagents, raising=True)
    (pkg_dir / "package.yaml").write_text(
        "name: computer-use\n"
        "kind: subagent\n"
        "version: 0.1.0\n"
        "prompt: prompt.md\n"
    )
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
    dependencies:
      - name: computer-use
        description: macOS UI operator
        package: computer-use
""",
    )
    P.spawn_pai(pid=1, slug="root", description="km")
    P.spawn_pai(pid=2, slug="pai", description="dflt")
    P.spawn_pai(
        pid=5,
        slug="pai.computer-use",
        description="macOS UI operator",
        parent=2,
        extra={"persistent": True, "persub": True},
    )

    C.reconcile_from_config()

    spec = P.read_spec("pai.computer-use")
    assert spec["package"] == "computer-use"


# ----- reconcile -----


def _seed_etc(repo_root: Path, body: str) -> None:
    _write_config(repo_root, body)


def test_reconcile_cold_boot(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    prompt: src/prompts/root.md
    model: deepseek-v4-pro
    wake_on: ['kernel:*']
  - name: pai
    pid: 2
    description: dflt
    prompt: src/prompts/pai_default.md
    model: deepseek-v4-pro
    wake_on: ['*']
""",
    )
    C.reconcile_from_config()
    actual = dict(P._iter_pai_specs())
    assert set(actual) == {"root", "pai"}
    assert actual["root"]["pid"] == 1
    assert actual["pai"]["pid"] == 2
    assert actual["pai"]["wake_on"] == ["*"]
    assert P.read_status("root") == "running"


def test_reconcile_add(repo_root, live_dir):
    # First reconcile: just the reserved pair.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    # Now add a third entry without a pid.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    description: a third one
""",
    )
    C.reconcile_from_config()
    actual = dict(P._iter_pai_specs())
    assert "extra" in actual
    assert actual["extra"]["pid"] >= 3


def test_reconcile_remove(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    pid: 5
    description: temp
""",
    )
    C.reconcile_from_config()
    assert P.read_status("extra") == "running"
    # Remove extra.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    # Proc dir preserved on disk; status flipped to cancelled.
    assert (live_dir / "proc" / "extra").exists()
    assert P.read_status("extra") == "cancelled"


def test_reconcile_change_rewrites_spec(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: original
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: updated
    wake_on: ['kernel:*']
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["description"] == "updated"
    assert spec["wake_on"] == ["kernel:*"]
    assert spec["pid"] == 1  # unchanged


def test_reconcile_pid_invariant(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    pid: 5
    description: temp
""",
    )
    C.reconcile_from_config()
    # Try to change extra's pid — should fail before any mutation.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    pid: 9
    description: temp
""",
    )
    with pytest.raises(C.ConfigError, match="cannot change"):
        C.reconcile_from_config()
    assert P.read_spec("extra")["pid"] == 5


def test_reconcile_preserves_unmanaged_fields(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    # Manually set an unmanaged field.
    spec_path = live_dir / "proc" / "root" / "spec.yaml"
    with spec_path.open() as f:
        spec = yaml.safe_load(f)
    spec["persistent"] = True
    with spec_path.open("w") as f:
        yaml.safe_dump(spec, f)
    # Reconcile with a change to a managed field.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: changed
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["persistent"] is True
    assert spec["description"] == "changed"
