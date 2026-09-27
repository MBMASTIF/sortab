"""Shared TreeStore <-> JSON(-able dict) serialization helpers.

Used by BOTH storage backends that need to round-trip a core.tree.TreeStore
through a byte/text store:
- backend/session_store.py   (ephemeral Redis "рабочая сессия")
- backend/projects_store.py  (persistent Postgres "Проект")

Split.fraction is a Decimal (neither JSON nor a plain dict has a Decimal
type), so it round-trips as a string and is parsed back through
Decimal(str(...)) — float never touches money on this path, in either
backend. Keeping exactly one copy of this logic means the two storage
backends can never silently drift on the wire format.
"""

from __future__ import annotations

from decimal import Decimal

from core.reconcile import Group, Split
from core.tree import TreeStore


def group_to_dict(g: Group) -> dict:
    return {"id": g.id, "parent_id": g.parent_id, "name": g.name}


def group_from_dict(d: dict) -> Group:
    return Group(id=d["id"], parent_id=d["parent_id"], name=d["name"])


def split_to_dict(s: Split) -> dict:
    return {"group_id": s.group_id, "fraction": str(s.fraction)}


def split_from_dict(d: dict) -> Split:
    return Split(group_id=d["group_id"], fraction=Decimal(d["fraction"]))


def tree_to_dict(tree: TreeStore) -> dict:
    """The exact shape stored in Project.tree_json: groups + assignment,
    nothing else (no raw data, per the architecture split in the README —
    Projects never hold a DataFrame)."""
    return {
        "groups": [group_to_dict(g) for g in tree.groups.values()],
        "assignment": {
            entity: [split_to_dict(s) for s in splits] for entity, splits in tree.assignment.items()
        },
    }


def tree_from_dict(d: dict) -> TreeStore:
    tree = TreeStore()
    tree.groups = {g["id"]: group_from_dict(g) for g in d["groups"]}
    tree.assignment = {
        entity: [split_from_dict(s) for s in splits] for entity, splits in d["assignment"].items()
    }
    return tree
