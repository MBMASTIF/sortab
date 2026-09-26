"""Mutable group-tree management: the CRUD/business-rule layer that sits
in front of core.reconcile's pure rollup computation.

Domain rules locked in during design (see README) and enforced here, not
just documented:
- An entity can only be assigned to a LEAF group (one with no children) —
  the user explicitly chose this over "any level" when asked directly.
- No group can be deleted while it still has children or directly-assigned
  entities — deletion must never silently lose data or orphan assignments;
  the caller has to explicitly clear it first.
- Assignment here is always a single whole group (fraction=1) — the
  "ножницы" partial-split tool is Phase 3, not part of this layer yet, but
  the output format (list[Split]) already matches what it will need.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from core.reconcile import Group, Split


class TreeError(ValueError):
    pass


class NotALeafError(TreeError):
    pass


class GroupNotEmptyError(TreeError):
    pass


@dataclass
class TreeStore:
    groups: dict[str, Group] = field(default_factory=dict)
    assignment: dict[str, list[Split]] = field(default_factory=dict)

    def add_group(self, group_id: str, name: str, parent_id: str | None = None) -> Group:
        if group_id in self.groups:
            raise TreeError(f"Group id {group_id!r} already exists")
        if parent_id is not None and parent_id not in self.groups:
            raise TreeError(f"Parent group {parent_id!r} does not exist")
        group = Group(id=group_id, parent_id=parent_id, name=name)
        self.groups[group_id] = group
        return group

    def is_leaf(self, group_id: str) -> bool:
        if group_id not in self.groups:
            raise TreeError(f"Unknown group {group_id!r}")
        return not any(g.parent_id == group_id for g in self.groups.values())

    def children_of(self, group_id: str) -> list[Group]:
        return [g for g in self.groups.values() if g.parent_id == group_id]

    def remove_group(self, group_id: str) -> None:
        if group_id not in self.groups:
            raise TreeError(f"Unknown group {group_id!r}")
        if self.children_of(group_id):
            raise GroupNotEmptyError(
                f"Group {group_id!r} still has subgroups — remove or move them first, "
                "deleting it now would silently orphan them"
            )
        assigned_here = [e for e, splits in self.assignment.items() if any(s.group_id == group_id for s in splits)]
        if assigned_here:
            raise GroupNotEmptyError(
                f"Group {group_id!r} still has {len(assigned_here)} entities assigned — "
                "unassign them first, deleting it now would silently lose the assignment"
            )
        del self.groups[group_id]

    def assign_entity(self, entity: str, group_id: str) -> None:
        """Entity is matched by its normalized form (caller's responsibility
        to normalize — strip/lower — before calling, same normalization
        used everywhere else so a value assigned once matches on reupload)."""
        if group_id not in self.groups:
            raise TreeError(f"Unknown group {group_id!r}")
        if not self.is_leaf(group_id):
            raise NotALeafError(
                f"Group {group_id!r} has subgroups — entities can only go into a leaf, "
                "pick one of its subgroups instead"
            )
        self.assignment[entity] = [Split(group_id=group_id, fraction=Decimal(1))]

    def unassign_entity(self, entity: str) -> None:
        self.assignment.pop(entity, None)

    def get_unassigned(self, all_entities: list[str]) -> list[str]:
        return [e for e in all_entities if e not in self.assignment]

    def detect_new_entities(self, current_entities: list[str], previously_known: set[str]) -> list[str]:
        """On re-upload: entities seen before (even if since unassigned by
        the user) are not "new" — only entities never encountered at all
        need the "5 new items found" prompt."""
        return [e for e in current_entities if e not in previously_known]

    def as_group_list(self) -> list[Group]:
        """Snapshot in the format core.reconcile.rollup() expects."""
        return list(self.groups.values())
