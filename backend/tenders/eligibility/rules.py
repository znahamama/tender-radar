"""Pure eligibility rules: post-validated extraction data + tender facts + profile -> decision.

No Django models or database here; tenders.eligibility.service does the storage.

Rules (RULES_VERSION "2"):
- Supply arrangement: tender.sa_required_from_metadata=True means "yes" whatever the LLM said
  (code precedence; the LLM answer stays stored and flagged). "yes" and the profile holds none
  of it -> blocker naming the arrangement if known.
- Clearance: a required level above the profile's -> blocker, except personnel Reliability,
  which is a warning (individual screening the firm can arrange). With the seeded profile
  (none/none) this is exactly: corporate not in (none, unknown) -> blocker; personnel
  secret/top_secret -> blocker; personnel reliability -> warning.
- On-site "yes": St. John's area -> fine (note); elsewhere in NL -> warning; outside NL ->
  blocker; both -> warning; place unknown -> warning "on-site, location not stated".
- Supplier origin -> note. Land claims "yes" -> warning. Documents behind login -> note.
- Status: blocked if any blocker; else check_solicitation if criteria_location is
  see_solicitation or the extraction failed; else no_blockers_stated.
- not_stated: the gates whose (effective) value is unknown, in schema order. Clearance counts
  when either side is unknown; SA does not when the metadata says it is required.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from tenders.extraction.schema import GATES
from tenders.extraction.validate import normalize_loose

from .places import Where, classify

RULES_VERSION = "2"
CLEARANCE_RANK = {"none": 0, "reliability": 1, "secret": 2, "top_secret": 3}
CLEARANCE_LABEL = {"none": "none", "reliability": "Reliability", "secret": "Secret",
                   "top_secret": "Top Secret"}
Status = Literal["blocked", "check_solicitation", "no_blockers_stated"]


@dataclass(frozen=True)
class Profile:
    name: str
    supply_arrangements: tuple[str, ...]
    corporate_clearance: str
    personnel_clearance: str

    @classmethod
    def from_model(cls, p) -> Profile:
        return cls(p.name, tuple(p.supply_arrangements), p.corporate_clearance,
                   p.personnel_clearance)


@dataclass(frozen=True)
class TenderFacts:
    sa_required_from_metadata: bool | None
    notice_type: str = ""


@dataclass(frozen=True)
class Finding:
    gate: str
    reason: str
    evidence: str | None = None


@dataclass
class Decision:
    status: Status
    blockers: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    notes: list[Finding] = field(default_factory=list)
    not_stated: list[str] = field(default_factory=list)

    def as_fields(self) -> dict:
        return {"status": self.status, "not_stated": list(self.not_stated),
                "blockers": [asdict(f) for f in self.blockers],
                "warnings": [asdict(f) for f in self.warnings],
                "notes": [asdict(f) for f in self.notes]}


_UNKNOWN_GATES = {
    "supply_arrangement_required": {"value": "unknown", "name": None, "evidence": None},
    "supplier_origin_restriction": {"value": "unknown", "evidence": None},
    "clearance": {"corporate": "unknown", "personnel": "unknown", "evidence": None},
    "work_location": {"place": None, "on_site": "unknown", "evidence": None},
    "land_claims_applies": {"value": "unknown", "evidence": None},
    "documents_behind_login": {"value": "unknown", "portal": None, "evidence": None},
}


def holds_arrangement(name: str, held: tuple[str, ...]) -> bool:
    """Loose match either way: "TBIPS" matches "TBIPS Tier 1"."""
    n = normalize_loose(name)
    return any(n and (n in h or h in n) for h in (normalize_loose(x) for x in held) if h)


def _supply_arrangement(g: dict, facts: TenderFacts, profile: Profile, d: Decision) -> str:
    value, name, evidence = g["value"], g["name"], g["evidence"]
    if facts.sa_required_from_metadata:
        if value != "yes":
            evidence = facts.notice_type or None
        value = "yes"
    if value != "yes":
        return value
    what = f"the {name} supply arrangement" if name else \
        "a supply arrangement or standing offer (not named)"
    if name and holds_arrangement(name, profile.supply_arrangements):
        d.notes.append(Finding("supply_arrangement_required",
                               f"Requires {what}; the profile holds it.", evidence))
    elif not name and profile.supply_arrangements:
        d.warnings.append(Finding("supply_arrangement_required",
                                  f"Requires {what}; check whether the profile's "
                                  "arrangements qualify.", evidence))
    else:
        d.blockers.append(Finding("supply_arrangement_required",
                                  f"Bidding is limited to holders of {what}; the profile "
                                  "holds none of it.", evidence))
    return value


def _clearance(g: dict, profile: Profile, d: Decision) -> bool:
    corp, pers, ev = g["corporate"], g["personnel"], g["evidence"]
    if corp in CLEARANCE_RANK and CLEARANCE_RANK[corp] > CLEARANCE_RANK[profile.corporate_clearance]:
        d.blockers.append(Finding(
            "clearance", f"Corporate clearance required: {CLEARANCE_LABEL[corp]} "
                         f"(profile: {CLEARANCE_LABEL[profile.corporate_clearance]}).", ev))
    if pers in CLEARANCE_RANK and CLEARANCE_RANK[pers] > CLEARANCE_RANK[profile.personnel_clearance]:
        finding = Finding(
            "clearance", f"Personnel clearance required: {CLEARANCE_LABEL[pers]} "
                         f"(profile: {CLEARANCE_LABEL[profile.personnel_clearance]}).", ev)
        (d.warnings if pers == "reliability" else d.blockers).append(finding)
    return "unknown" in (corp, pers)


def _work_location(g: dict, d: Decision) -> str:
    on_site, place, ev = g["on_site"], g["place"], g["evidence"]
    if on_site != "yes":
        return on_site
    gate = "work_location"
    if not place:
        d.warnings.append(Finding(gate, "On-site work, location not stated.", ev))
        return on_site
    where = classify(place)
    if where is Where.OUTSIDE_NL:
        d.blockers.append(Finding(gate, f"On-site work at {place}, outside NL.", ev))
    elif where is Where.ST_JOHNS_AREA:
        d.notes.append(Finding(gate, f"On-site work at {place} (St. John's area).", ev))
    elif where is Where.NL_OTHER:
        d.warnings.append(Finding(gate, f"On-site work at {place}, elsewhere in NL.", ev))
    elif where is Where.MIXED:
        d.warnings.append(Finding(gate, f"On-site work at {place}: some locations are "
                                        "outside NL.", ev))
    else:
        d.warnings.append(Finding(gate, f"On-site work at {place}: could not tell whether "
                                        "it is in NL.", ev))
    return on_site


_ORIGIN_NOTES = {
    "canadian_only": "Limited to Canadian suppliers, goods or services.",
    "canadian_and_trading_partners": "Open to Canadian suppliers and trade-agreement partners.",
    "none": "No supplier-origin restriction stated.",
}


def evaluate(data: dict | None, flags: list[str], facts: TenderFacts, profile: Profile,
             extraction_error: str = "") -> Decision:
    """``data`` is the post-validated extraction (None if the extraction failed)."""
    d = Decision(status="check_solicitation")
    gates = (data or {}).get("eligibility") or _UNKNOWN_GATES

    unknown = {
        "supply_arrangement_required":
            _supply_arrangement(gates["supply_arrangement_required"], facts, profile, d)
            == "unknown",
        "clearance": _clearance(gates["clearance"], profile, d),
        "work_location": _work_location(gates["work_location"], d) == "unknown",
        "supplier_origin_restriction": gates["supplier_origin_restriction"]["value"] == "unknown",
        "land_claims_applies": gates["land_claims_applies"]["value"] == "unknown",
        "documents_behind_login": gates["documents_behind_login"]["value"] == "unknown",
    }
    d.not_stated = [g for g in GATES if unknown[g]]

    origin = gates["supplier_origin_restriction"]
    if origin["value"] in _ORIGIN_NOTES:
        d.notes.append(Finding("supplier_origin_restriction", _ORIGIN_NOTES[origin["value"]],
                               origin["evidence"]))
    land = gates["land_claims_applies"]
    if land["value"] == "yes":
        d.warnings.append(Finding("land_claims_applies", "A comprehensive land claims "
                                  "agreement applies.", land["evidence"]))
    docs = gates["documents_behind_login"]
    if docs["value"] == "yes":
        d.notes.append(Finding("documents_behind_login", "Documents are behind a login: "
                               f"{docs['portal'] or 'portal not named'}.", docs["evidence"]))
    if data is None:
        d.warnings.append(Finding("extraction", "Extraction failed"
                                  + (f": {extraction_error.splitlines()[0]}"
                                     if extraction_error else ".")))
    if "injection_suspected" in flags:
        d.warnings.append(Finding("extraction", "The notice text may try to instruct the "
                                                "model; read it yourself."))

    if d.blockers:
        d.status = "blocked"
    elif data is None or data.get("criteria_location") == "see_solicitation":
        d.status = "check_solicitation"
    else:
        d.status = "no_blockers_stated"
    return d
