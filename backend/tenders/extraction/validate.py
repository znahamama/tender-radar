"""Code-side checks on schema-valid LLM output: evidence grounding and metadata cross-checks.

Pure and cheap: it runs again on every read of an extraction (see service.assess), so a
rule changed here applies to cached rows without an API call.
"""

from __future__ import annotations

import unicodedata

from .prompt import META_PREFIX, NOT_READ, Notice
from .schema import NOTICE_SOURCE, ExtractionData

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
    or it is exactly one whole metadata value (e.g. a date or a notice type), bare or with
    its rendered label ("closing_date: 2026-11-13T19:00:00+00:00")."""
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


class _Grounding:
    """Where a quote may come from. Schema 1 output (v2, v3) has no ``source``: every quote
    is checked against the notice. In schema 2 (v4) only gate evidence names one: "notice"
    checks the notice; a document name checks that document's whole stored text, and only a
    document the model was given (full or partial). A gate quote not in the source it names
    but in another source given is kept, with its source corrected (``locate``). A list
    item's quote (no source) is grounded in the notice or in any document given."""

    def __init__(self, notice: Notice):
        self.notice = [normalize(text) for _, text in notice.sources]
        # A whole metadata value, bare or as the exact "label: value" line the notice renders.
        meta = [(kind.removeprefix(META_PREFIX), text) for kind, text in notice.sources
                if kind.startswith(META_PREFIX)]
        self.whole_values = frozenset(normalize(form) for label, text in meta
                                      for form in (text, f"{label}: {text}"))
        given = [d for d in notice.documents if d.status != NOT_READ]
        self.documents = {normalize(d.name): normalize(d.text) for d in given}
        self.names = {normalize(d.name): d.name for d in given}  # in input order
        doc_loose = [normalize_loose(d.text) for d in given]
        self.details = [normalize_loose(text) for _, text in notice.sources] + doc_loose
        self.places = [normalize_loose(text) for kind, text in notice.sources
                       if kind not in REGION_SOURCES] + doc_loose

    def quote(self, evidence: str | None, source: str | None) -> bool:
        if source is None or normalize(source) == NOTICE_SOURCE:
            return is_grounded(evidence, self.notice, self.whole_values)
        text = self.documents.get(normalize(source))
        return text is not None and is_grounded(evidence, [text])

    def locate(self, evidence: str | None) -> str | None:
        """The first source given (the notice, then the documents in input order) that the
        quote is grounded in, same thresholds; None if none. A whole metadata value counts
        only for the notice, as in quote()."""
        if is_grounded(evidence, self.notice, self.whole_values):
            return NOTICE_SOURCE
        return next((self.names[key] for key, text in self.documents.items()
                     if is_grounded(evidence, [text])), None)

    def anywhere(self, evidence: str | None) -> bool:
        """In the notice or in any document given; with no documents (v2, v3), the notice."""
        return (is_grounded(evidence, self.notice, self.whole_values)
                or is_grounded(evidence, list(self.documents.values())))


def _drop_evidence(item) -> None:
    """Schema 2 sources are never null: no quote means "notice" (schema.SOURCE_GATE)."""
    item.evidence = None
    if hasattr(item, "source"):
        item.source = NOTICE_SOURCE


def postvalidate(data: ExtractionData, notice: Notice,
                 sa_required_from_metadata: bool | None) -> tuple[ExtractionData, list[str]]:
    """Return a corrected copy of ``data`` and the flags raised.

    Sources are the title, each metadata value and the description; never labels or tags.
    With prompt v4, also the documents the model was given: a gate quote must sit in the
    one source it names, a list item's quote in the notice or any document given (see
    _Grounding), with the same word and character thresholds.
    - Gate evidence not in the source it names (v4) but in another source given: kept,
      source -> where it was found (notice first, then documents in input order), flag
      ``source_corrected:eligibility.<gate>``.
    - Gate evidence not grounded: evidence -> None, values -> "unknown", details -> None,
      flag ``ungrounded:eligibility.<gate>``. A non-unknown value with no evidence counts.
    - A quote equal to a whole metadata value is grounded whatever its length.
    - A detail (SA name, work place, portal) not found in the notice (or, with v4, in a
      document given), ignoring punctuation and case: -> None, flag
      ``ungrounded_detail:eligibility.<gate>.<field>``. A work place found only in a regions
      metadata value does not count. (A grounded quote is itself inside a source, so
      "in its own quote or in the notice" reduces to "in a permitted source".)
    - List items with ungrounded evidence are dropped, flag ``ungrounded:<list>[<i>]``.
    """
    data = data.model_copy(deep=True)
    where = _Grounding(notice)
    flags: list[str] = []

    for gate, (value_fields, detail_fields) in _GATES.items():
        g = getattr(data.eligibility, gate)
        claims = any(getattr(g, f) != "unknown" for f in value_fields)
        source = getattr(g, "source", None)
        if ((g.evidence is not None or claims) and source is not None
                and not where.quote(g.evidence, source)
                and (found := where.locate(g.evidence)) is not None):
            g.source = found  # schema 2 only: schema 1 quotes name no source
            flags.append(f"source_corrected:eligibility.{gate}")
        if ((g.evidence is not None or claims)
                and not where.quote(g.evidence, getattr(g, "source", None))):
            _drop_evidence(g)
            for f in value_fields:
                setattr(g, f, "unknown")
            for f in detail_fields:
                setattr(g, f, None)
            flags.append(f"ungrounded:eligibility.{gate}")
        elif g.evidence is None and getattr(g, "source", NOTICE_SOURCE) != NOTICE_SOURCE:
            g.source = NOTICE_SOURCE  # a source with no quote names nothing
        for f in detail_fields:
            value = getattr(g, f)
            allowed = where.places if f == "place" else where.details
            if value is not None and not _appears(value, allowed):
                setattr(g, f, None)
                flags.append(f"ungrounded_detail:eligibility.{gate}.{f}")

    for name in _LISTS:
        kept = []
        for i, item in enumerate(getattr(data, name)):
            if where.anywhere(item.evidence):
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
