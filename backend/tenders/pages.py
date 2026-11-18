"""Public, read-only HTML pages: the tender list ("/") and one tender ("/tenders/<ref>/").

GET only, and nothing here writes: the pages show what the daily run stored. The
assessment shown is the newest one, for the default (fictional) profile, of an extraction
by the configured LLM (settings.LLM_MODEL); the keyword baseline sits beside it on the
detail page for comparison only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.db.models.fields.json import KT
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from tenders.extraction.keyword import KeywordExtractor
from tenders.extraction.schema import GATES
from tenders.ingest.injection import INJECTION_PATTERN
from tenders.ingest.schema import SOURCE_TZ
from tenders.models import Assessment, Extraction, SupplierProfile, Tender

PAGE_SIZE = 25
MAX_QUERY_CHARS = 200
NOT_ASSESSED = "not_assessed"

STATUS_LABELS = {**dict(Assessment.Status.choices), NOT_ASSESSED: "Not assessed"}

GATE_LABELS = {
    "supply_arrangement_required": "Supply arrangement required",
    "supplier_origin_restriction": "Supplier origin restriction",
    "clearance": "Security clearance",
    "work_location": "Work location",
    "land_claims_applies": "Land claims agreement",
    "documents_behind_login": "Documents behind a login",
    "extraction": "Extraction",
}
assert set(GATES) <= set(GATE_LABELS)

_ORIGIN = {"none": "none", "canadian_only": "Canadian only",
           "canadian_and_trading_partners": "Canadian and trade-agreement partners",
           "unknown": "unknown"}
_CLEARANCE = {"none": "none", "reliability": "Reliability", "secret": "Secret",
              "top_secret": "Top Secret", "unknown": "unknown"}
_ON_SITE = {"yes": "on-site", "no": "not on-site", "unknown": "unknown"}
_DETAIL_FIELDS = {"name": "arrangement name", "place": "place", "portal": "portal"}


# --- queries --------------------------------------------------------------------------

def _default_profile_id() -> Subquery:
    """The lowest profile id, as a subquery (no extra round trip, and no error if missing)."""
    return Subquery(SupplierProfile.objects.order_by("id").values("id")[:1])


def primary_assessments() -> QuerySet[Assessment]:
    """Assessments the pages show: default profile, configured LLM, newest extraction first."""
    return (Assessment.objects
            .filter(profile_id=_default_profile_id(), extraction__model_id=settings.LLM_MODEL)
            .order_by("-extraction__created_at", "-extraction_id"))


def is_open() -> Q:
    """Open at the last ingest, and the closing date (if any) hasn't passed since."""
    return (Q(tender_status__iexact="Open")
            & (Q(closing_date__isnull=True) | Q(closing_date__gte=timezone.now())))


def tender_rows() -> QuerySet[Tender]:
    newest = primary_assessments().filter(tender=OuterRef("pk"))
    return Tender.objects.annotate(
        eligibility_status=Subquery(newest.values("status")[:1]),
        first_blocker_label=Subquery(
            newest.annotate(r=KT("blockers__0__label")).values("r")[:1]),
        first_blocker_gate=Subquery(newest.annotate(r=KT("blockers__0__gate")).values("r")[:1]),
    )


def blocker_label(row: Tender) -> str:
    """Short form of a row's first blocker. Findings from before rules v3 have no label:
    fall back to the gate's name (the next assess_tenders run adds the label)."""
    if row.first_blocker_label:
        return row.first_blocker_label
    if row.first_blocker_gate:
        return GATE_LABELS.get(row.first_blocker_gate, row.first_blocker_gate)
    return ""


# --- list -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Filters:
    show: str  # "open" or "all"
    status: str
    region: str
    q: str

    @classmethod
    def from_request(cls, get) -> Filters:
        """Unknown values are ignored rather than rejected: a stale link still works."""
        show = "all" if get.get("show") == "all" else "open"
        status = get.get("status", "")
        region = get.get("region", "")
        return cls(
            show=show,
            status=status if status in STATUS_LABELS else "",
            region=region if region in Tender.RegionScope.values else "",
            q=" ".join(get.get("q", "").split())[:MAX_QUERY_CHARS],
        )

    def apply(self, qs: QuerySet[Tender]) -> QuerySet[Tender]:
        if self.show == "open":
            qs = qs.filter(is_open()).order_by(F("closing_date").asc(nulls_last=True), "id")
        else:
            qs = qs.order_by(F("closing_date").desc(nulls_last=True), "id")
        if self.status == NOT_ASSESSED:
            qs = qs.filter(eligibility_status__isnull=True)
        elif self.status:
            qs = qs.filter(eligibility_status=self.status)
        if self.region:
            qs = qs.filter(region_scope=self.region)
        if self.q:
            qs = qs.filter(title__icontains=self.q)
        return qs


@require_GET
def tender_list(request):
    filters = Filters.from_request(request.GET)
    page = Paginator(filters.apply(tender_rows()), PAGE_SIZE).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list)
    for row in page.object_list:
        row.first_blocker = blocker_label(row)
    return render(request, "tenders/list.html", {
        "page": page,
        "filters": filters,
        "status_choices": list(STATUS_LABELS.items()),
        "region_choices": Tender.RegionScope.choices,
        "filtered": bool(filters.status or filters.region or filters.q),
    })


# --- detail ---------------------------------------------------------------------------

def gate_value(gate: str, g: dict | None) -> str:
    """One gate's answer as a short phrase."""
    if not g:
        return ""
    if gate == "supply_arrangement_required":
        return g["value"] + (f" ({g['name']})" if g.get("name") else "")
    if gate == "supplier_origin_restriction":
        return _ORIGIN.get(g["value"], g["value"])
    if gate == "clearance":
        if g["corporate"] == g["personnel"] == "unknown":
            return "unknown"
        return (f"corporate: {_CLEARANCE.get(g['corporate'], g['corporate'])}; "
                f"personnel: {_CLEARANCE.get(g['personnel'], g['personnel'])}")
    if gate == "work_location":
        return _ON_SITE.get(g["on_site"], g["on_site"]) + (f", {g['place']}" if g.get("place")
                                                           else "")
    if gate == "documents_behind_login":
        return g["value"] + (f" ({g['portal']})" if g.get("portal") else "")
    return g.get("value", "")


def gate_flags(gate: str, flags: list[str]) -> list[str]:
    """The post-validation flags about one gate, in words."""
    out = []
    for flag in flags:
        kind, _, where = flag.partition(":")
        if kind == "ungrounded" and where == f"eligibility.{gate}":
            out.append("quote not found in the notice: treated as unknown")
        elif kind == "ungrounded_detail" and where.startswith(f"eligibility.{gate}."):
            field = where.rsplit(".", 1)[1]
            out.append(f"{_DETAIL_FIELDS.get(field, field)} not found in the notice: dropped")
    if gate == "supply_arrangement_required" and "sa_metadata_disagrees" in flags:
        out.append("disagrees with the notice metadata, which requires an arrangement")
    return out


def _eligibility(extraction: Extraction | None, source: str) -> dict:
    data = getattr(extraction, source, None) if extraction else None
    return (data or {}).get("eligibility") or {}


@dataclass(frozen=True)
class GateRow:
    gate: str
    label: str
    value: str
    raw_value: str  # what the model said, when post-validation changed it
    evidence: str | None
    flags: list[str]
    baseline: str


def gate_rows(llm: Extraction | None, baseline: Extraction | None) -> list[GateRow]:
    checked, raw = _eligibility(llm, "data"), _eligibility(llm, "raw_output")
    base = _eligibility(baseline, "data")
    flags = list(llm.flags) if llm else []
    rows = []
    for gate in GATES:
        value = gate_value(gate, checked.get(gate))
        said = gate_value(gate, raw.get(gate))
        rows.append(GateRow(
            gate=gate, label=GATE_LABELS[gate], value=value,
            raw_value=said if said != value else "",
            evidence=(checked.get(gate) or {}).get("evidence"),
            flags=gate_flags(gate, flags),
            baseline=gate_value(gate, base.get(gate)),
        ))
    return rows


_EXTRACTION_FLAGS = {
    "truncated": "The description was too long and was cut before extraction.",
    "stale_input": "The notice text changed after this extraction.",
    "injection_suspected": "The notice text may try to instruct the model; read it yourself.",
    "api_retry": "The API call was retried.",
    "validation_retry": "The first answer was invalid and was retried.",
    "raw_output_invalid": "The stored answer no longer fits the schema.",
}


def extraction_flags(flags: list[str]) -> list[str]:
    """Flags about the extraction as a whole, in words. Gate flags are in the table, and
    dropped list items (deliverables etc.) aren't shown on this page."""
    return [_EXTRACTION_FLAGS.get(f, f) for f in flags
            if not f.startswith(("ungrounded:", "ungrounded_detail:", "sa_metadata_disagrees"))]


INJECTION_CAUTION = ("Parts of this notice look like instructions to an AI system; treat its "
                     "extracted fields with extra care.")


def injection_caution(tender: Tender, llm: Extraction | None) -> bool:
    """The code-side detector flagged the notice at ingest, or the model said the notice
    tries to instruct it. Either way the page shows one neutral note; nothing else changes."""
    return (INJECTION_PATTERN in (tender.data_quality or [])
            or bool(llm and "injection_suspected" in (llm.flags or [])))


def labelled(findings: list[dict]) -> list[dict]:
    """Assessment findings ({gate, reason, evidence}) with a readable gate label."""
    return [{**f, "gate_label": GATE_LABELS.get(f.get("gate"), f.get("gate"))} for f in findings]


def published_date(tender: Tender) -> date | None:
    """The CSV's publication date is date-only; ingest stores it as midnight in SOURCE_TZ.
    Convert back in that zone (not St. John's) so the day is the one the source gave."""
    if tender.publication_date is None:
        return None
    return tender.publication_date.astimezone(SOURCE_TZ).date()


def tender_is_open(tender: Tender) -> bool:
    """is_open() for one row, without a query."""
    return (tender.tender_status.lower() == "open"
            and (tender.closing_date is None or tender.closing_date >= timezone.now()))


def official_url(tender: Tender) -> str | None:
    """Only ever an https link: autoescaping doesn't stop a javascript: URL."""
    url = (tender.notice_url or "").strip()
    return url if url.lower().startswith("https://") else None


@require_GET
def tender_detail(request, reference: str):
    tender = get_object_or_404(Tender, reference_number=reference)
    assessment = (primary_assessments().filter(tender=tender)
                  .select_related("extraction").first())
    llm = assessment.extraction if assessment else (
        tender.extractions.filter(model_id=settings.LLM_MODEL)
        .order_by("-created_at", "-id").first())
    baseline = (tender.extractions
                .filter(model_id=KeywordExtractor.model_id, status=Extraction.Status.OK)
                .order_by("-created_at", "-id").first())
    return render(request, "tenders/detail.html", {
        "tender": tender,
        "published": published_date(tender),
        "official_url": official_url(tender),
        "is_open": tender_is_open(tender),
        "assessment": assessment,
        "blockers": labelled(assessment.blockers if assessment else []),
        "warnings": labelled(assessment.warnings if assessment else []),
        "notes": labelled(assessment.notes if assessment else []),
        "not_stated": [GATE_LABELS.get(g, g) for g in (assessment.not_stated if assessment
                                                        else [])],
        "llm": llm,
        "baseline": baseline,
        "rows": gate_rows(llm, baseline),
        "other_flags": extraction_flags(llm.flags if llm else []),
        "injection_caution": INJECTION_CAUTION if injection_caution(tender, llm) else "",
    })
