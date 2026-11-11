"""Code-side checks on schema-valid LLM output: evidence grounding and metadata cross-checks."""

from __future__ import annotations

import unicodedata

from .schema import ExtractionData

_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " "})

# Per gate: the value fields reset to "unknown" and the detail fields reset to None.
_GATE_VALUES = {
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


def is_grounded(evidence: str | None, source_norm: str) -> bool:
    if evidence is None:
        return False
    ev = normalize(evidence)
    return bool(ev) and ev in source_norm


def postvalidate(data: ExtractionData, source_text: str,
                 sa_required_from_metadata: bool | None) -> tuple[ExtractionData, list[str]]:
    """Return a corrected copy of ``data`` and the flags raised.

    - Gate evidence not found in the source: evidence -> None, values -> "unknown",
      details (name/place/portal) -> None, flag ``ungrounded:eligibility.<gate>``.
      A non-unknown gate value with no evidence at all is treated the same way.
    - List items (deliverables etc.) with ungrounded evidence are dropped, flag
      ``ungrounded:<list>[<i>]`` (index in the model's output).
    """
    data = data.model_copy(deep=True)
    source = normalize(source_text)
    flags: list[str] = []

    for gate, (value_fields, detail_fields) in _GATE_VALUES.items():
        g = getattr(data.eligibility, gate)
        claims = any(getattr(g, f) != "unknown" for f in value_fields)
        if g.evidence is None and not claims:
            continue
        if not is_grounded(g.evidence, source):
            g.evidence = None
            for f in value_fields:
                setattr(g, f, "unknown")
            for f in detail_fields:
                setattr(g, f, None)
            flags.append(f"ungrounded:eligibility.{gate}")

    for name in _LISTS:
        kept = []
        for i, item in enumerate(getattr(data, name)):
            if is_grounded(item.evidence, source):
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
