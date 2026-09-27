"""Reconciliation between two files ("Сверка взаиморасчётов" — README Фаза
3 / Каталог услуг item 3): matches entities between file A and file B and
reports, per entity, whether the summed metric agrees, differs, or is
missing on one side.

Matching is two-pass, cheapest-first:

1. Exact match on a normalized key (trim + collapse whitespace + lowercase)
   — the overwhelming majority of real reconciliation pairs (same
   counterparty name typed the same way in both files) resolve here with
   no fuzzy comparison at all.
2. Whatever is left on both sides goes through RapidFuzz, blocked by the
   digits extracted from each entity string before any fuzzy comparison
   runs — the same pattern the README's benchmark validated (45 000 unique
   values: 51s naive all-pairs vs 0.09s blocked). Entities that share no
   digits at all only ever get compared to other digit-less entities, not
   to the whole opposite list.

Money is Decimal throughout — see core/reconcile.py for why float never
touches a row value anywhere in this project.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz, process

FUZZY_SCORE_CUTOFF = 92.0  # 0-100, RapidFuzz's scale

STATUS_MATCH = "совпадает"
STATUS_MISMATCH = "расхождение"
STATUS_ONLY_A = "только в A"
STATUS_ONLY_B = "только в B"


@dataclass(frozen=True)
class CompareRow:
    entity: str  # display label — the file-A spelling if matched or only-A, else file-B's
    entity_a: str | None
    entity_b: str | None
    sum_a: Decimal | None
    sum_b: Decimal | None
    diff: Decimal | None  # sum_b - sum_a, only set when both sides matched
    status: str


def _aggregate(entities: list[str], values: list[Decimal]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for entity, value in zip(entities, values):
        totals[entity] = totals.get(entity, Decimal(0)) + value
    return totals


def _normalize(s: str) -> str:
    return " ".join(s.strip().lower().split())


def _digit_key(s: str) -> str:
    return "".join(re.findall(r"\d+", s))


def _make_row(a_key: str, b_key: str, a_val: Decimal, b_val: Decimal) -> CompareRow:
    diff = b_val - a_val
    status = STATUS_MATCH if diff == 0 else STATUS_MISMATCH
    return CompareRow(
        entity=a_key, entity_a=a_key, entity_b=b_key, sum_a=a_val, sum_b=b_val, diff=diff, status=status
    )


def _fuzzy_match(keys_a: list[str], keys_b: list[str], threshold: float) -> dict[str, str]:
    """Greedy one-to-one matching: every A key blocked to the B keys that
    share its digit-key, best RapidFuzz score above `threshold` wins.
    Deterministic (A keys visited in sorted order) so results don't depend
    on dict/set iteration order."""
    buckets_b: dict[str, list[str]] = defaultdict(list)
    for key in keys_b:
        buckets_b[_digit_key(key)].append(key)

    used_b: set[str] = set()
    matches: dict[str, str] = {}
    for a_key in sorted(keys_a):
        candidates = [k for k in buckets_b.get(_digit_key(a_key), []) if k not in used_b]
        if not candidates:
            continue
        best = process.extractOne(a_key, candidates, scorer=fuzz.WRatio, score_cutoff=threshold)
        if best is None:
            continue
        match_str, _score, _idx = best
        matches[a_key] = match_str
        used_b.add(match_str)
    return matches


def compare_entities(
    entities_a: list[str],
    values_a: list[Decimal],
    entities_b: list[str],
    values_b: list[Decimal],
    threshold: float = FUZZY_SCORE_CUTOFF,
) -> list[CompareRow]:
    """Aggregates each file's rows by entity (summing duplicates within the
    same file), matches entities across files, and returns one CompareRow
    per matched pair or per unmatched entity on either side. Row order is
    entity name, ascending, for a stable/testable result."""
    totals_a = _aggregate(entities_a, values_a)
    totals_b = _aggregate(entities_b, values_b)

    # Pass 1: exact match on normalized key. First-wins on a normalization
    # collision within file B (e.g. two literally different B rows that
    # both normalize the same) — rare, and no worse than leaving one of
    # them to (correctly) fall through to "только в B".
    norm_b_lookup: dict[str, str] = {}
    for key in totals_b:
        norm_b_lookup.setdefault(_normalize(key), key)

    rows: list[CompareRow] = []
    matched_b: set[str] = set()
    unmatched_a: dict[str, Decimal] = {}
    for a_key, a_val in totals_a.items():
        b_key = norm_b_lookup.get(_normalize(a_key))
        if b_key is not None and b_key not in matched_b:
            matched_b.add(b_key)
            rows.append(_make_row(a_key, b_key, a_val, totals_b[b_key]))
        else:
            unmatched_a[a_key] = a_val

    unmatched_b = {key: val for key, val in totals_b.items() if key not in matched_b}

    # Pass 2: fuzzy, blocked by digits, only over what pass 1 couldn't place.
    if unmatched_a and unmatched_b:
        fuzzy_pairs = _fuzzy_match(list(unmatched_a.keys()), list(unmatched_b.keys()), threshold)
        for a_key, b_key in fuzzy_pairs.items():
            rows.append(_make_row(a_key, b_key, unmatched_a[a_key], unmatched_b[b_key]))
        for a_key in fuzzy_pairs:
            del unmatched_a[a_key]
        for b_key in fuzzy_pairs.values():
            del unmatched_b[b_key]

    for a_key, a_val in unmatched_a.items():
        rows.append(
            CompareRow(entity=a_key, entity_a=a_key, entity_b=None, sum_a=a_val, sum_b=None, diff=None, status=STATUS_ONLY_A)
        )
    for b_key, b_val in unmatched_b.items():
        rows.append(
            CompareRow(entity=b_key, entity_a=None, entity_b=b_key, sum_a=None, sum_b=b_val, diff=None, status=STATUS_ONLY_B)
        )

    rows.sort(key=lambda r: r.entity)
    return rows
