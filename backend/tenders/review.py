"""Human review of extractions: validate a correction, and apply decisions before the rules.

A ReviewDecision is append-only; the newest one per (extraction, gate) counts:
- accept: the model's post-validated value stands.
- correct: the reviewer's value and evidence replace the model's, BEFORE the frozen rules
  (tenders.eligibility.rules) run. tenders.eligibility.service.assess() calls apply_reviews().
- reject: the model's value is wrong and the gate is treated as unknown. criteria_location
  has no "unknown", so a rejected criteria_location becomes "see_solicitation" (the
  conservative answer: check the solicitation).

A correction is checked exactly like model output: its values must be allowed by the frozen
schema (tenders.extraction.schema), and its evidence and details go through the same
post-validation (tenders.extraction.validate.postvalidate) against the same notice text the
model saw (tenders.extraction.prompt.build_notice).

The supply-arrangement metadata override stays a rule: if the notice metadata says an
arrangement is required, rules v3 say "yes" whatever the gate value, human or model.

No API calls, and nothing worker-only is imported: the public detail page uses this module.
"""

from __future__ import annotations

import copy
import typing
from types import UnionType

from django.core.exceptions import ValidationError
from pydantic import BaseModel
from pydantic import ValidationError as SchemaError

from tenders.extraction.prompt import build_notice
from tenders.extraction.schema import GATES, Eligibility, ExtractionData
from tenders.extraction.validate import postvalidate
from tenders.models import Extraction, ReviewDecision

CRITERIA = "criteria_location"
REVIEW_GATES = (*GATES, CRITERIA)
REJECTED_CRITERIA = "see_solicitation"
Action = ReviewDecision.Action


# --- the frozen schema, read rather than restated --------------------------------------

def gate_model(gate: str) -> type[BaseModel]:
    return Eligibility.model_fields[gate].annotation


def _literal(annotation) -> tuple[str, ...] | None:
    return typing.get_args(annotation) if typing.get_origin(annotation) is typing.Literal else None


def value_fields(gate: str) -> dict[str, tuple[str, ...]]:
    """The gate's enumerated fields and their allowed values, in schema order."""
    if gate == CRITERIA:
        return {"value": _literal(ExtractionData.model_fields[CRITERIA].annotation)}
    return {name: allowed for name, f in gate_model(gate).model_fields.items()
            if (allowed := _literal(f.annotation))}


def detail_fields(gate: str) -> tuple[str, ...]:
    """Free-text details (SA name, work place, portal): optional strings, not evidence."""
    if gate == CRITERIA:
        return ()
    return tuple(name for name, f in gate_model(gate).model_fields.items()
                 if name != "evidence" and isinstance(f.annotation, UnionType))


def unknown_gate(gate: str) -> dict:
    """The gate as "not stated": every value unknown, details and evidence null."""
    return {name: ("unknown" if name in value_fields(gate) else None)
            for name in gate_model(gate).model_fields}


def _main_field(gate: str) -> str:
    """The value field a gate's details belong to (clearance has no details)."""
    return "on_site" if gate == "work_location" else "value"


# --- decisions -------------------------------------------------------------------------

def latest_decisions(extraction: Extraction) -> dict[str, ReviewDecision]:
    """The decision that counts per gate: the newest. Uses a prefetch when there is one
    (ReviewDecision's default ordering is newest first within each gate)."""
    latest: dict[str, ReviewDecision] = {}
    for d in extraction.review_decisions.all():
        current = latest.get(d.gate)
        if current is None or (d.created_at, d.pk) > (current.created_at, current.pk):
            latest[d.gate] = d
    return latest


def overridden_value(decision: ReviewDecision):
    """What a correct or reject decision puts in place of the model's answer."""
    if decision.gate == CRITERIA:
        return (decision.corrected_value["value"] if decision.action == Action.CORRECT
                else REJECTED_CRITERIA)
    if decision.action == Action.CORRECT:
        return {**decision.corrected_value, "evidence": decision.evidence}
    return unknown_gate(decision.gate)


def apply_reviews(data: dict | None, decisions: dict[str, ReviewDecision]) -> dict | None:
    """The post-validated extraction with each gate's latest correct/reject applied (a copy).
    A failed extraction (None) stays None: there is no model answer to review."""
    if data is None:
        return None
    out = copy.deepcopy(data)
    for gate, d in decisions.items():
        if d.action == Action.ACCEPT:
            continue
        if gate == CRITERIA:
            out[CRITERIA] = overridden_value(d)
        else:
            out["eligibility"][gate] = overridden_value(d)
    return out


# --- validating a correction -----------------------------------------------------------

def _probe(gate: str, gate_value: dict) -> ExtractionData:
    """A minimal schema-valid extraction holding one gate, to run the real post-validation
    on (the other gates are unknown, so they raise no flags)."""
    eligibility = {g: unknown_gate(g) for g in GATES}
    eligibility[gate] = gate_value
    return ExtractionData.model_validate({
        "summary": "", "work_type": "other", "criteria_location": REJECTED_CRITERIA,
        "deliverables": [], "procurement_method": None, "deadlines": [],
        "mandatory_requirements": [], "certifications": [], "eligibility": eligibility,
        "injection_suspected": False,
    })


def validate_correction(extraction: Extraction, gate: str, fields: dict,
                        evidence: str = "") -> tuple[dict, str]:
    """Check a reviewer's correction; return (corrected_value, evidence) to store.

    Raises django ValidationError keyed by field name (value fields, detail fields,
    "evidence", or "__all__"), so a form shows each error next to its input.
    """
    if gate not in REVIEW_GATES:
        raise ValidationError(f"unknown gate {gate!r}")
    values = {f: fields.get(f) for f in value_fields(gate)}
    for f, allowed in value_fields(gate).items():
        if values[f] not in allowed:
            raise ValidationError({f: f"{values[f]!r} is not one of {', '.join(allowed)}"})

    if gate == CRITERIA:
        return {"value": values["value"]}, ""

    if all(v == "unknown" for v in values.values()):
        raise ValidationError("A correction states a value. To say the model's answer is wrong "
                              "and leave the gate unknown, use Reject.")
    details = {f: (fields.get(f) or "").strip() or None for f in detail_fields(gate)}
    main = _main_field(gate)
    for f, d in details.items():
        if d is not None and values[main] != "yes":
            raise ValidationError({f: f"only filled when {main} is yes"})
    evidence = " ".join((evidence or "").split())
    if not evidence:
        raise ValidationError({"evidence": "a value other than unknown needs evidence: an "
                                           "exact quote from the notice"})

    try:
        checked = gate_model(gate).model_validate({**values, **details, "evidence": evidence})
    except SchemaError as e:
        err = e.errors()[0]
        raise ValidationError({str(err["loc"][0]): err["msg"]}) from None

    tender = extraction.tender
    _, flags = postvalidate(_probe(gate, checked.model_dump()), build_notice(tender),
                            tender.sa_required_from_metadata)
    errors = {}
    if f"ungrounded:eligibility.{gate}" in flags:
        errors["evidence"] = ("not an exact quote of the notice: 3+ words and 15+ characters "
                              "from one place in the title, a metadata value or the "
                              "description, or one whole metadata value")
    for f in detail_fields(gate):
        if f"ungrounded_detail:eligibility.{gate}.{f}" in flags:
            where = " (not counting the regions values)" if f == "place" else ""
            errors[f] = f"does not appear in the notice{where}"
    if errors:
        raise ValidationError(errors)
    stored = checked.model_dump()
    return {k: v for k, v in stored.items() if k != "evidence"}, stored["evidence"]
