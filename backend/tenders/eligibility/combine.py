"""Per-gate combine of a tender's documents extraction (prompt v4 or v5: notice and
solicitation documents) with its v3 one (notice only); "v4" below means either. No API
calls and no writes: both extractions are already stored and post-validated (assess()
refreshes them first).

For each eligibility gate ("grounded" = a quote survived post-validation):
- v4 grounded -> v4's answer;
- v4 not grounded, v3 grounded -> v3's answer, its quote shown as from the notice
  (flag ``from_v3:eligibility.<gate>``);
- both grounded and different values -> v4's answer, flag
  ``versions_disagree:eligibility.<gate>`` (the notice goes in the review queue);
- otherwise v4's answer.
Everything else (criteria_location, the lists) is v4's. Human review decisions are applied
after this, so they still override either version.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from tenders.extraction.prompt import (
    DOCUMENTS_PROMPT_VERSION,
    DOCUMENTS_PROMPTS,
    PROMPT_VERSION,
)
from tenders.extraction.schema import GATES, NOTICE_SOURCE
from tenders.models import Extraction
from tenders.review import value_fields

# DOCS: the documents prompt the site uses now; a combine names the one it was given.
DOCS, V3 = DOCUMENTS_PROMPT_VERSION, PROMPT_VERSION
FROM_V3 = "from_v3"
VERSIONS_DISAGREE = "versions_disagree"


@dataclass(frozen=True)
class Combined:
    data: dict | None
    versions: dict[str, str] = field(default_factory=dict)  # gate -> "v4"/"v5" | V3; {}
    flags: list[str] = field(default_factory=list)

    def record(self, partner: Extraction | None) -> dict:
        """What Assessment.combined stores: {} when there was no v3 extraction to combine."""
        if partner is None:
            return {}
        return {"v3_extraction": partner.pk, "gates": self.versions, "flags": self.flags}


def notice_only_partner(extraction: Extraction) -> Extraction | None:
    """The newest ok v3 extraction of the same tender and model, for a documents extraction."""
    if extraction.prompt_version not in DOCUMENTS_PROMPTS:
        return None
    return (Extraction.objects
            .filter(tender_id=extraction.tender_id, model_id=extraction.model_id,
                    prompt_version=V3, status=Extraction.Status.OK)
            .order_by("-created_at", "-id").first())


def _grounded(g: dict | None) -> bool:
    return bool(g) and g.get("evidence") is not None


def _answer(gate: str, g: dict) -> dict:
    """The values a status depends on (not the quote, source or details such as a place)."""
    return {f: g.get(f) for f in value_fields(gate)}


def combine(v4_data: dict | None, v3_data: dict | None, docs_version: str = DOCS) -> Combined:
    """Pure: the post-validated v4 data with v3 answers filled in per gate (a copy).
    ``docs_version``: the documents extraction's prompt version, as recorded per gate."""
    if v4_data is None or v3_data is None:
        return Combined(v4_data)
    out = copy.deepcopy(v4_data)
    e4, e3 = out["eligibility"], v3_data.get("eligibility") or {}
    versions, flags = {}, []
    for gate in GATES:
        g4, g3 = e4.get(gate), e3.get(gate)
        if _grounded(g4):
            versions[gate] = docs_version
            if _grounded(g3) and _answer(gate, g3) != _answer(gate, g4):
                flags.append(f"{VERSIONS_DISAGREE}:eligibility.{gate}")
        elif _grounded(g3):
            e4[gate] = {**copy.deepcopy(g3), "source": NOTICE_SOURCE}
            versions[gate] = V3
            flags.append(f"{FROM_V3}:eligibility.{gate}")
        else:
            versions[gate] = docs_version
    return Combined(out, versions, flags)


def combined_for(extraction: Extraction | None, record: dict | None
                 ) -> tuple[Combined, Extraction | None]:
    """Re-derive what an assessment used, from its stored record (for the pages, which
    never write). The v3 extraction's stored data is what assess() last refreshed."""
    data = extraction.data if extraction else None
    partner_id = (record or {}).get("v3_extraction")
    partner = (Extraction.objects.filter(pk=partner_id).first()
               if partner_id and data is not None else None)
    if partner is None:
        return Combined(data), None
    return combine(data, partner.data, extraction.prompt_version), partner
