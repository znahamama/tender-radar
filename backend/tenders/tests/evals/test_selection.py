import random

import pytest

from tenders.evals.selection import (
    QUOTAS,
    Candidate,
    select,
    split,
    strata_counts,
    strata_for,
)


def test_strata_from_metadata_and_text():
    s = strata_for(notice_kind="against_sa", region_scope="nl", tender_status="Open",
                   title="Network upgrade",
                   description="Personnel need Reliability Status. Excludes land-claims areas.")
    assert s == {"against_sa", "region_nl", "status_open", "text_security", "text_land_claims"}
    s = strata_for(notice_kind="regular", region_scope="nl+national", tender_status="Cancelled",
                   title="Cybersecurity tools", description="Landscaping claims nothing.")
    assert s == frozenset()  # "cybersecurity" is not the word "security"


def pool(n=200, seed=1):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        s = {rng.choice(["region_nl", "region_national", None]),
             rng.choice(["status_open", "status_expired", "status_expired"])}
        for name, p in (("against_sa", .12), ("sa_or_so_request", .2), ("text_security", .3),
                        ("text_land_claims", .1)):
            if rng.random() < p:
                s.add(name)
        if {"against_sa", "sa_or_so_request"} <= s:
            s.discard("sa_or_so_request")
        out.append(Candidate(f"cb-{i:04d}", frozenset(s - {None})))
    return out


def test_select_meets_quotas_and_is_deterministic():
    candidates = pool()
    picked = select(candidates, seed=7)
    assert len(picked) == 30 and len({p.candidate.reference for p in picked}) == 30
    counts = strata_counts(picked)
    assert all(counts[s] >= q for s, q in QUOTAS.items())
    again = select(list(reversed(candidates)), seed=7)  # input order doesn't matter
    assert [p.candidate.reference for p in again] == [p.candidate.reference for p in picked]
    assert [p.candidate.reference for p in select(candidates, seed=8)] != \
        [p.candidate.reference for p in picked]


def test_split_spreads_strata():
    picked = select(pool(), seed=7)
    dev, test = split(picked, seed=7)
    assert len(dev) == 10 and len(test) == 20
    d, t = strata_counts(dev), strata_counts(test)
    assert all(d[s] > 0 and t[s] > 0 for s in QUOTAS)
    assert split(picked, seed=7) == (dev, test)


def test_select_raises_when_pool_too_thin():
    thin = [Candidate(f"cb-{i}", frozenset({"region_nl"})) for i in range(40)]
    with pytest.raises(ValueError, match="cannot meet quotas"):
        select(thin)
