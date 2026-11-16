"""Stratified, seeded selection of eval cases and the dev/test split.

Strata come from Tender metadata and keyword matches on the notice text (title and
description) only, never from model output. Pure: eval/select_cases.py does the I/O.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

SEED = 20261116
N_CASES, N_DEV = 30, 10
MIN_WORDS = 120

# stratum -> minimum number of selected cases
QUOTAS = {
    "against_sa": 4,
    "sa_or_so_request": 3,
    "region_nl": 8,
    "region_national": 8,
    "text_security": 5,
    "text_land_claims": 3,
    "status_open": 10,
    "status_expired": 10,
}
STRATUM_RULES = {
    "against_sa": 'notice_kind is "against_sa" (code-side, from notice_type metadata)',
    "sa_or_so_request": 'notice_kind is "sa_or_so_request" (code-side, from metadata)',
    "region_nl": 'region_scope is "nl" ("nl+national" counts for neither region stratum)',
    "region_national": 'region_scope is "national"',
    "text_security": "title or description matches \\b(security|clearance|reliability)\\b, "
                     "case-insensitive",
    "text_land_claims": "title or description matches \\bland[\\s-]+claims?\\b, "
                        "case-insensitive",
    "status_open": 'tender_status is "Open" at selection time',
    "status_expired": 'tender_status is "Expired" at selection time',
}
_SECURITY = re.compile(r"\b(security|clearance|reliability)\b", re.IGNORECASE)
_LAND_CLAIMS = re.compile(r"\bland[\s-]+claims?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    reference: str
    strata: frozenset[str]


def strata_for(*, notice_kind: str, region_scope: str, tender_status: str, title: str,
               description: str) -> frozenset[str]:
    s = set()
    if notice_kind in ("against_sa", "sa_or_so_request"):
        s.add(notice_kind)
    if region_scope == "nl":
        s.add("region_nl")
    elif region_scope == "national":
        s.add("region_national")
    text = f"{title}\n{description}"
    if _SECURITY.search(text):
        s.add("text_security")
    if _LAND_CLAIMS.search(text):
        s.add("text_land_claims")
    if tender_status.lower() in ("open", "expired"):
        s.add(f"status_{tender_status.lower()}")
    return frozenset(s)


@dataclass(frozen=True)
class Picked:
    candidate: Candidate
    picked_for: str  # the stratum quota it was drawn for, or "fill"


def select(candidates: list[Candidate], seed: int = SEED, n: int = N_CASES) -> list[Picked]:
    """Scarcest stratum first, draw from a seeded shuffle until each quota is met (cases
    already drawn count toward every stratum they belong to); then fill to ``n`` from the
    same shuffle. Raises if the pool cannot meet the quotas in ``n`` cases."""
    order = sorted(candidates, key=lambda c: c.reference)
    rng = random.Random(seed)
    rng.shuffle(order)
    pool_counts = {s: sum(s in c.strata for c in order) for s in QUOTAS}
    picked: list[Picked] = []
    taken: set[str] = set()

    def count(s: str) -> int:
        return sum(s in p.candidate.strata for p in picked)

    for stratum in sorted(QUOTAS, key=lambda s: (pool_counts[s] / QUOTAS[s], s)):
        for c in order:
            if count(stratum) >= QUOTAS[stratum]:
                break
            if c.reference not in taken and stratum in c.strata:
                picked.append(Picked(c, stratum))
                taken.add(c.reference)
    for c in order:
        if len(picked) >= n:
            break
        if c.reference not in taken:
            picked.append(Picked(c, "fill"))
            taken.add(c.reference)

    short = {s: f"{count(s)}/{q}" for s, q in QUOTAS.items() if count(s) < q}
    if len(picked) != n or short:
        raise ValueError(f"cannot meet quotas in {n} cases: picked {len(picked)}, short {short}")
    return picked


def split(picked: list[Picked], seed: int = SEED, n_dev: int = N_DEV,
          tries: int = 20_000) -> tuple[list[Picked], list[Picked]]:
    """Seeded search for the split whose per-stratum dev share is closest to n_dev/n, with
    every stratum present on both sides. Returns (dev, test), each in a random order."""
    rng = random.Random(f"{seed}-split")
    share = n_dev / len(picked)
    totals = {s: sum(s in p.candidate.strata for p in picked) for s in QUOTAS}
    best, best_score = None, None
    for _ in range(tries):
        order = picked[:]
        rng.shuffle(order)
        dev = order[:n_dev]
        counts = {s: sum(s in p.candidate.strata for p in dev) for s in QUOTAS}
        if any(totals[s] >= 2 and not (0 < counts[s] < totals[s]) for s in QUOTAS):
            continue
        score = sum((counts[s] - totals[s] * share) ** 2 for s in QUOTAS)
        if best_score is None or score < best_score:
            best, best_score = order, score
    if best is None:
        raise ValueError("no split puts every stratum on both sides")
    return best[:n_dev], best[n_dev:]


def strata_counts(cases: list[Picked]) -> dict[str, int]:
    return {s: sum(s in p.candidate.strata for p in cases) for s in QUOTAS}
