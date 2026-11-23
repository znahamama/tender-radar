"""Unread-text tripwire. Eligibility rules v4 = rules v3 (rules.py, places.py, unchanged)
followed by this module; the three files are pinned together in test_frozen.py.

Pure, no Django. tenders.eligibility.service finds the stored document text the model did
not see (past a cut, a document left out at the cap, or every ok document when the
extraction read none) and passes it here as UnreadText.

Rules (RULES_VERSION "4"), applied after evaluate() when its status is not "blocked":
- clearance not stated (either side unknown) and the unread text says Reliability Status,
  Secret, Top Secret, Designated Organization Screening, Facility Security Clearance,
  "contains a security requirement" or "security requirement(s) associated" (not after
  "no": "There are no security requirements associated with ..." is the standard wording
  for none) -> a clearance warning;
- work location not stated and the unread text says on-site, on site or sur place -> a
  work-location warning.
Each warning names the first hit (term, document, approximate page) and how many more there
are. Any warning makes no_blockers_stated check_solicitation. Never a blocker, never a
change to a stated gate: a notice whose clearance is answered (e.g. "none") is left alone.
Case-insensitive; whitespace (including line breaks) may separate the words of a term.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from . import rules
from .rules import Decision, Finding

RULES_VERSION = "4"
BASE_RULES_VERSION = rules.RULES_VERSION  # "3": what evaluate() applies before this

# (gate, term as reported, pattern). Within a gate, the first alternative that matches at a
# position wins, so "Top Secret" is one hit, not also "Secret".
TERMS: tuple[tuple[str, str, str], ...] = (
    ("clearance", "Reliability Status", r"reliability\s+status"),
    ("clearance", "Top Secret", r"top\s+secret"),
    ("clearance", "Secret", r"secret"),
    ("clearance", "Designated Organization Screening",
     r"designated\s+organi[sz]ation\s+screening"),
    ("clearance", "Facility Security Clearance", r"facility\s+security\s+clearance"),
    ("clearance", "contains a security requirement", r"contains\s+a\s+security\s+requirement"),
    ("clearance", "security requirements associated",
     r"(?<!\bno\s)security\s+requirements?\s+associated"),
    ("work_location", "on-site", r"on[\-‐‑]site"),
    ("work_location", "on site", r"on\s+site"),
    ("work_location", "sur place", r"sur\s+place"),
)
GATES = ("clearance", "work_location")
_PATTERNS = {
    gate: re.compile("|".join(rf"(?P<t{i}>\b(?:{rx})\b)" for i, (g, _, rx) in enumerate(TERMS)
                              if g == gate), re.IGNORECASE)
    for gate in GATES
}
_NOT_STATED = {"clearance": "Clearance is not stated in what the model read",
               "work_location": "The work location is not stated in what the model read"}


@dataclass(frozen=True)
class UnreadText:
    document: str  # the document's name (as in the extraction's input when it was there)
    text: str  # its whole stored text
    start: int  # where the part the model did not see begins (0: it saw none of it)
    pages: int | None = None  # pages the stored text covers, for the approximate page


@dataclass(frozen=True)
class Hit:
    gate: str
    term: str
    document: str
    page: int | None  # approximate: by the hit's position in the text


def _page(u: UnreadText, offset: int) -> int | None:
    if not u.pages or not u.text:
        return None
    return 1 + min(u.pages - 1, offset * u.pages // len(u.text))


def find_hits(unread: Iterable[UnreadText]) -> list[Hit]:
    """Every term hit in the unread part of each text, per gate in document then text order."""
    hits: list[Hit] = []
    unread = list(unread)
    for gate in GATES:
        for u in unread:
            region = u.text[u.start:]
            for m in _PATTERNS[gate].finditer(region):
                term = TERMS[int(m.lastgroup[1:])][1]
                hits.append(Hit(gate, term, u.document, _page(u, u.start + m.start())))
    return hits


def _reason(gate: str, hits: list[Hit]) -> str:
    first = hits[0]
    where = f"{first.document}" + (f", about page {first.page}" if first.page else "")
    more = f" ({len(hits) - 1} more mention(s) unread)" if len(hits) > 1 else ""
    return (f"{_NOT_STATED[gate]}, but text it did not read says “{first.term}” "
            f"({where}){more}. Check the solicitation.")


def apply(decision: Decision, hits: list[Hit]) -> Decision:
    """The decision with a warning per open gate that has hits; no_blockers_stated becomes
    check_solicitation if there is any. A new Decision; the one given is not changed."""
    if decision.status == "blocked":
        return decision
    warnings = []
    for gate in GATES:
        found = [h for h in hits if h.gate == gate]
        if gate in decision.not_stated and found:
            warnings.append(Finding(gate, _reason(gate, found), None,
                                    f"Unread text: {found[0].term}"))
    if not warnings:
        return decision
    status = "check_solicitation" if decision.status == "no_blockers_stated" else decision.status
    return replace(decision, status=status, warnings=[*decision.warnings, *warnings],
                   blockers=list(decision.blockers), notes=list(decision.notes),
                   not_stated=list(decision.not_stated))


def needed(decision: Decision) -> bool:
    """Whether apply() could change this decision (so the caller can skip reading texts)."""
    return decision.status != "blocked" and any(g in decision.not_stated for g in GATES)
