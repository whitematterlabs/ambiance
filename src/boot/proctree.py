"""Parent/child tree assembly over /proc records.

Generic helper shared by `bin/ps` and the TUI ProcWatcher. Caller supplies
records that expose a `pid` (or other id) and an optional `parent`; we
return them in pre-order traversal with a tree-prefix string per row
suitable for prepending to a display column.

A record is anything indexable by string key (dict, dataclass-as-dict).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable


def _pid_sort(record: dict, key: str) -> int:
    val = record.get(key)
    return val if isinstance(val, int) else 1 << 30


def order_as_tree(
    records: Iterable[dict],
    *,
    id_key: str = "pid",
    parent_key: str = "parent",
) -> list[tuple[dict, str]]:
    """Return [(record, prefix), ...] in tree order.

    Roots come first (sorted by id); each root's descendants follow in
    pre-order. `prefix` uses box-drawing chars (`├─ `, `└─ `, `│  `,
    `   `); roots get an empty prefix. Records whose `parent` points to
    an unknown id are surfaced as roots (orphans) so they don't vanish.
    """
    records = list(records)
    by_id: dict[int, dict] = {
        r[id_key]: r for r in records if isinstance(r.get(id_key), int)
    }
    children: dict[int, list[dict]] = {}
    roots: list[dict] = []
    for r in records:
        parent = r.get(parent_key)
        if isinstance(parent, int) and parent in by_id:
            children.setdefault(parent, []).append(r)
        else:
            roots.append(r)
    for kids in children.values():
        kids.sort(key=lambda r: _pid_sort(r, id_key))
    roots.sort(key=lambda r: _pid_sort(r, id_key))

    out: list[tuple[dict, str]] = []

    def walk(record: dict, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            connector = ""
            child_prefix = ""
        else:
            connector = "└─ " if is_last else "├─ "
            child_prefix = "   " if is_last else "│  "
        out.append((record, prefix + connector))
        rid = record.get(id_key)
        kids = children.get(rid, []) if isinstance(rid, int) else []
        for i, kid in enumerate(kids):
            walk(kid, prefix + child_prefix, i == len(kids) - 1, False)

    for root in roots:
        walk(root, "", True, True)
    return out


def is_orphan(record: dict, by_id: dict, parent_key: str = "parent") -> bool:
    parent = record.get(parent_key)
    return isinstance(parent, int) and parent not in by_id
