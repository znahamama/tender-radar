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
post-validation (tenders.extraction.validate.postvalidate) against the same input the
extraction read (tenders.extraction.prompt.build_input for its prompt version): the notice
for v2/v3, the notice and the documents it was given for v4/v5. With documents, the quote's
source is found as for a model quote (the notice first, then the documents in input order)
and stored with the correction, so the public page names the document it came from.

Decisions follow a notice across re-extractions (carry_forward, CarriedReview): with the
same LLM input they carry as they stood; with changed input each gate is re-checked, and a
gate whose decision no longer holds is marked "needs re-review (notice text changed)".

The supply-arrangement metadata override stays a rule: if the notice metadata says an
arrangement is required, rules v3 say "yes" whatever the gate value, human or model.

No API calls, and nothing worker-only is imported: the public detail page uses this module.
"""

from __future__ import annotations

import copy
import typing
from dataclasses import dataclass, field
from itertools import pairwise
from types import UnionType

from django.core.exceptions import ValidationError
from pydantic import BaseModel
from pydantic import ValidationError as SchemaError

from tenders.extraction.prompt import build_input
from tenders.extraction.schema import (
    GATES,
    NOTICE_SOURCE,
    SCHEMA_VERSION,
    SCHEMAS,
    Eligibility,
    ExtractionData,
)
from tenders.extraction.validate import postvalidate
from tenders.models import CarriedReview, Extraction, ReviewDecision

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

def _schema(extraction: Extraction) -> type[ExtractionData]:
    """The output schema the extraction was made with: 2 (v4/v5) names each quote's source."""
    return SCHEMAS.get(extraction.schema_version or SCHEMA_VERSION, ExtractionData)


def _probe(gate: str, gate_value: dict, schema: type[ExtractionData] = ExtractionData
           ) -> ExtractionData:
    """A minimal schema-valid extraction holding one gate, to run the real post-validation
    on (the other gates are unknown, so they raise no flags). With schema 2 every gate
    names the notice as its source; postvalidate moves a quote found only in a document
    to that document, as it does for the model's."""
    sourced = schema is not ExtractionData
    eligibility = {g: unknown_gate(g) | ({"source": NOTICE_SOURCE} if sourced else {})
                   for g in GATES}
    eligibility[gate] = gate_value | ({"source": NOTICE_SOURCE} if sourced else {})
    return schema.model_validate({
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
    schema = _schema(extraction)
    with_documents = schema is not ExtractionData
    data, flags = postvalidate(_probe(gate, checked.model_dump(), schema),
                               build_input(tender, extraction.prompt_version),
                               tender.sa_required_from_metadata)
    errors = {}
    where = "the notice or a document this extraction was given" if with_documents \
        else "the notice"
    if f"ungrounded:eligibility.{gate}" in flags:
        errors["evidence"] = (f"not an exact quote of {where}: 3+ words and 15+ characters "
                              "from one place in the title, a metadata value, the "
                              "description" + (" or one document" if with_documents else "")
                              + ", or one whole metadata value")
    for f in detail_fields(gate):
        if f"ungrounded_detail:eligibility.{gate}.{f}" in flags:
            regions = " (not counting the regions values)" if f == "place" else ""
            errors[f] = f"does not appear in {where}{regions}"
    if errors:
        raise ValidationError(errors)
    stored = checked.model_dump()
    value = {k: v for k, v in stored.items() if k != "evidence"}
    if with_documents:  # where the quote was found, as for a model quote
        value["source"] = getattr(data.eligibility, gate).source
    return value, stored["evidence"]


# --- decisions across re-extractions ---------------------------------------------------

Reason = CarriedReview.Reason
NEEDS_REREVIEW = "needs re-review (notice text changed)"


@dataclass
class Reviews:
    """What review means for one extraction.

    ``applied``: the decision in effect per gate: this extraction's own newest decision,
    else a carried one that still holds (the person's original ReviewDecision either way).
    ``carried``: the newest CarriedReview per gate that has no own decision (applied or
    not), for "from an earlier version" and "needs re-review".
    """

    applied: dict[str, ReviewDecision] = field(default_factory=dict)
    carried: dict[str, CarriedReview] = field(default_factory=dict)

    @property
    def needs_rereview(self) -> list[str]:
        return [g for g in REVIEW_GATES if g in self.carried and not self.carried[g].applied]


def latest_carried(extraction_id: int) -> dict[str, CarriedReview]:
    """Queried, not read from a prefetch: carry_forward() may have just added rows."""
    latest: dict[str, CarriedReview] = {}
    rows = (CarriedReview.objects.filter(extraction_id=extraction_id)
            .select_related("source__reviewer", "source__extraction")
            .order_by("created_at", "id"))
    for c in rows:
        latest[c.gate] = c
    return latest


def reviews_for(extraction: Extraction) -> Reviews:
    own = latest_decisions(extraction)
    carried = {g: c for g, c in latest_carried(extraction.pk).items() if g not in own}
    applied = {**{g: c.source for g, c in carried.items() if c.applied}, **own}
    return Reviews(applied=applied, carried=carried)


def _model_answer(extraction: Extraction, gate: str):
    """The model's post-validated answer for a gate, without its evidence or the evidence's
    source (prompt v4 names one; a v3 answer has none, and the answer itself is the same)."""
    data = extraction.data or {}
    if gate == CRITERIA:
        return data.get(CRITERIA)
    g = (data.get("eligibility") or {}).get(gate) or {}
    return {k: v for k, v in g.items() if k not in ("evidence", "source")}


def _judge(source: ReviewDecision, inherited: CarriedReview | None, prev: Extraction,
           cur: Extraction) -> tuple[bool, str]:
    """Does ``source`` (in effect on ``prev``) still hold on ``cur``?"""
    if prev.input_hash and prev.input_hash == cur.input_hash:
        # Same text: nothing to re-check; a decision carries exactly as it stood.
        return (True, Reason.SAME_INPUT) if inherited is None else (inherited.applied,
                                                                     inherited.reason)
    if source.action == Action.CORRECT:
        if source.gate == CRITERIA:
            return False, Reason.NO_QUOTE
        try:
            validate_correction(cur, source.gate, dict(source.corrected_value), source.evidence)
        except ValidationError:
            return False, Reason.QUOTE_GONE
        return True, Reason.QUOTE_FOUND
    # accept / reject judged the model's answer on the extraction they were made on.
    same = _model_answer(source.extraction, source.gate) == _model_answer(cur, source.gate)
    return (True, Reason.SAME_ANSWER) if same else (False, Reason.ANSWER_CHANGED)


def _carry_step(prev: Extraction, cur: Extraction) -> None:
    """Judge, for ``cur``, each gate's decision as it stands on ``prev``: prev's own newest
    decision, else the newest one carried onto prev (applied or not)."""
    own_prev = latest_decisions(prev)
    carried_prev = latest_carried(prev.pk)
    own_cur = latest_decisions(cur)
    have = latest_carried(cur.pk)
    for gate in REVIEW_GATES:
        if gate in own_cur:
            continue  # a person reviewed this version itself
        inherited = None if gate in own_prev else carried_prev.get(gate)
        source = own_prev.get(gate) or (inherited.source if inherited else None)
        if source is None:
            continue
        if gate in have and have[gate].source_id == source.pk:
            continue  # already judged for this source
        applied, reason = _judge(source, inherited, prev, cur)
        CarriedReview.objects.create(extraction=cur, gate=gate, source=source,
                                     applied=applied, reason=reason)


def carry_forward(extraction: Extraction) -> None:
    """Carry review decisions along this notice's ok extractions (same model), oldest first,
    up to ``extraction``. Idempotent; one query when the notice has never been reviewed.
    Called by tenders.eligibility.service.assess() before it reads the decisions."""
    if extraction.status != Extraction.Status.OK:
        return
    same_notice = Extraction.objects.filter(tender_id=extraction.tender_id,
                                            model_id=extraction.model_id)
    if not ReviewDecision.objects.filter(extraction__in=same_notice).exists():
        return
    chain = list(same_notice.filter(status=Extraction.Status.OK)
                 .select_related("tender").order_by("created_at", "id"))
    chain = chain[: next(i for i, x in enumerate(chain) if x.pk == extraction.pk) + 1]
    for prev, cur in pairwise(chain):
        _carry_step(prev, cur)
