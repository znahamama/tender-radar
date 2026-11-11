"""Code-side checks on schema-valid LLM output: evidence grounding and metadata cross-checks.

Pure and cheap: it runs again on every read of an extraction (see service.assess), so a
rule changed here applies to cached rows without an API call.
"""

from __future__ import annotations

import unicodedata

from .prompt import META_PREFIX, Notice
from .schema import ExtractionData

# Read at call time (not bound as defaults) so tests and future tuning can change them.
MIN_EVIDENCE_WORDS = 3
MIN_EVIDENCE_CHARS = 15
REGION_SOURCES = frozenset({META_PREFIX + "regions_of_delivery",
                            META_PREFIX + "regions_of_opportunity"})

_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " ",
                        "<": "‹", ">": "›"})  # model may "un-neutralize" brackets

# Per gate: value fields reset to "unknown", and detail fields that must appear in the notice.
_GATES = {
    "supply_arrangement_required": (("value",), ("name",)),
    "supplier_origin_restriction": (("value",), ()),
    "clearance": (("corporate", "personnel"), ()),
    "work_location": (("on_site",), ("place",)),
    "land_claims_applies": (("value",), ()),
    "documents_behind_login": (("value",), ("portal",)),
}
_LISTS = ("deliverables", "deadlines", "mandatory_requirements", "certifications")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_PUNCT)
    return " ".join(s.split()).casefold()


def normalize_loose(s: str) -> str:
    """For detail fields only: normalize() and also treat every punctuation mark as a space."""
    s = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in normalize(s))
    return " ".join(s.split())


def is_grounded(evidence: str | None, sources: list[str],
                whole_values: frozenset[str] = frozenset()) -> bool:
    """Evidence sits inside one normalized source segment and is 3+ words and 15+ chars,
    or it is exactly one whole metadata value (e.g. a date or a notice type)."""
    if evidence is None:
        return False
    ev = normalize(evidence)
    if ev and ev in whole_values:
        return True
    if len(ev) < MIN_EVIDENCE_CHARS or len(ev.split()) < MIN_EVIDENCE_WORDS:
        return False
    return any(ev in s for s in sources)


def _appears(detail: str, loose_sources: list[str]) -> bool:
    d = normalize_loose(detail)
    return bool(d) and any(d in s for s in loose_sources)


def postvalidate(data: ExtractionData, notice: Notice,
                 sa_required_from_metadata: bool | None) -> tuple[ExtractionData, list[str]]:
    """Return a corrected copy of ``data`` and the flags raised.

    Sources are the title, each metadata value and the description; never labels or tags.
    - Gate evidence not grounded: evidence -> None, values -> "unknown", details -> None,
      flag ``ungrounded:eligibility.<gate>``. A non-unknown value with no evidence counts.
    - A quote equal to a whole metadata value is grounded whatever its length.
    - A detail (SA name, work place, portal) not found in the notice, ignoring punctuation
      and case: -> None, flag
      ``ungrounded_detail:eligibility.<gate>.<field>``. A work place found only in a regions
      metadata value does not count. (A grounded quote is itself inside a source, so
      "in its own quote or in the notice" reduces to "in a permitted source".)
    - List items with ungrounded evidence are dropped, flag ``ungrounded:<list>[<i>]``.
    """
    data = data.model_copy(deep=True)
    all_sources = [normalize(text) for _, text in notice.sources]
    whole_values = frozenset(normalize(text) for kind, text in notice.sources
                             if kind.startswith(META_PREFIX))
    detail_sources = [normalize_loose(text) for _, text in notice.sources]
    place_sources = [normalize_loose(text) for kind, text in notice.sources
                     if kind not in REGION_SOURCES]
    flags: list[str] = []

    for gate, (value_fields, detail_fields) in _GATES.items():
        g = getattr(data.eligibility, gate)
        claims = any(getattr(g, f) != "unknown" for f in value_fields)
        if ((g.evidence is not None or claims)
                and not is_grounded(g.evidence, all_sources, whole_values)):
            g.evidence = None
            for f in value_fields:
                setattr(g, f, "unknown")
            for f in detail_fields:
                setattr(g, f, None)
            flags.append(f"ungrounded:eligibility.{gate}")
        for f in detail_fields:
            value = getattr(g, f)
            allowed = place_sources if f == "place" else detail_sources
            if value is not None and not _appears(value, allowed):
                setattr(g, f, None)
                flags.append(f"ungrounded_detail:eligibility.{gate}.{f}")

    for name in _LISTS:
        kept = []
        for i, item in enumerate(getattr(data, name)):
            if is_grounded(item.evidence, all_sources, whole_values):
                kept.append(item)
            else:
                flags.append(f"ungrounded:{name}[{i}]")
        setattr(data, name, kept)

    if (sa_required_from_metadata is True
            and data.eligibility.supply_arrangement_required.value != "yes"):
        flags.append("sa_metadata_disagrees")
    if data.injection_suspected:
        flags.append("injection_suspected")
    return data, flags
