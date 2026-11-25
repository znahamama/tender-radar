"""Public, read-only HTML pages: the tender list ("/") and one tender ("/tenders/<ref>/").

GET and HEAD only, and nothing here writes: the pages show what the daily run stored. The
assessment shown is that of the current extraction (the one a run last produced or re-used,
Extraction.last_used_at) by the configured LLM (settings.LLM_MODEL), for the default
(fictional) profile; the keyword baseline sits beside it on the detail page for comparison
only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import (
    Count,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
)
from django.db.models.fields.json import KT
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_safe

from tenders.documents.fetch import is_allowed_url
from tenders.eligibility.combine import V3, Combined, combined_for
from tenders.extraction.prompt import DOCUMENTS_PROMPTS
from tenders.extraction.schema import GATES, NOTICE_SOURCE
from tenders.ingest.injection import INJECTION_PATTERN
from tenders.ingest.schema import SOURCE_TZ
from tenders.models import (
    Assessment,
    CategoryCode,
    Extraction,
    ReviewDecision,
    SupplierProfile,
    Tender,
)
from tenders.review import apply_reviews, reviews_for

PAGE_SIZE = 25
IN_SCOPE = "in"  # tenders.scope.IN; tests/test_scope.py pins the two equal
SCOPE_LIST_MAX = 10  # codes listed on the detail page's scope line
# KeywordExtractor.model_id, as a literal: importing keyword.py pulls in providers.py and
# the Anthropic SDK (~0.6 s of every API cold start). tests/test_pages.py pins the two equal.
BASELINE_MODEL_ID = "keyword-baseline"
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


def without_nul(value: str) -> str:
    """User input without NUL characters. Postgres can't store or compare text holding one
    (psycopg raises), so a "\x00" in a search or a reference was a server error."""
    return value.replace("\x00", "")


def reference_or_404(reference: str) -> str:
    """A reference from a URL path, NUL-free; nothing left -> 404 (public and admin)."""
    reference = without_nul(reference)
    if not reference:
        raise Http404("No such notice.")
    return reference


# --- queries --------------------------------------------------------------------------

def _default_profile_id() -> Subquery:
    """The lowest profile id, as a subquery (no extra round trip, and no error if missing)."""
    return Subquery(SupplierProfile.objects.order_by("id").values("id")[:1])


def primary_assessments() -> QuerySet[Assessment]:
    """Assessments the pages show: default profile, configured LLM, current extraction first
    (the one last produced or re-used by a run: Extraction.last_used_at)."""
    return (Assessment.objects
            .filter(profile_id=_default_profile_id(), extraction__model_id=settings.LLM_MODEL)
            .order_by("-extraction__last_used_at", "-extraction_id"))


def current_extractions(tender, model_id: str | None = None) -> QuerySet[Extraction]:
    """A tender's extractions by one model (default: the configured LLM), the current one
    first: the one last produced or re-used by a run (Extraction.last_used_at), the same
    order as primary_assessments. ``tender``: a Tender, its pk or an OuterRef."""
    return (Extraction.objects.filter(tender=tender, model_id=model_id or settings.LLM_MODEL)
            .order_by("-last_used_at", "-id"))


def current_extraction(tender, model_id: str | None = None) -> Extraction | None:
    """The extraction the site shows for ``tender`` (None if it has none)."""
    return current_extractions(tender, model_id).first()


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
        first_warning_label=Subquery(
            newest.annotate(r=KT("warnings__0__label")).values("r")[:1]),
        first_warning_gate=Subquery(newest.annotate(r=KT("warnings__0__gate")).values("r")[:1]),
        not_stated_count=Subquery(newest.annotate(
            r=Func(F("not_stated"), function="jsonb_array_length", output_field=IntegerField())
        ).values("r")[:1]),
        scope_status=Subquery(newest.annotate(r=KT("scope__status")).values("r")[:1]),
    )


def _short_label(label: str | None, gate: str | None) -> str:
    """A finding's short form. Findings from before rules v3, and gates that never get a
    label (land claims, extraction), fall back to the gate's name."""
    if label:
        return label
    if gate:
        return GATE_LABELS.get(gate, gate)
    return ""


def blocker_label(row: Tender) -> str:
    """Short form of a row's first blocker (the next assess_tenders run adds old labels)."""
    return _short_label(row.first_blocker_label, row.first_blocker_gate)


def open_reason(row: Tender) -> str:
    """What is still open on a check_solicitation row, in a few words: its first warning,
    else how many gates the notice leaves unstated. Empty for every other status."""
    if row.eligibility_status != Assessment.Status.CHECK_SOLICITATION:
        return ""
    warning = _short_label(row.first_warning_label, row.first_warning_gate)
    if warning:
        return warning
    n = row.not_stated_count or 0
    return f"{n} gate{'' if n == 1 else 's'} not stated" if n else ""


def status_summary(filters: Filters) -> tuple[int, list[dict]]:
    """The list's triage summary: notices per eligibility status under every filter except
    the status one (so each count is what its link would show), and their total. One
    GROUP BY query. "Not assessed" is listed only when there is one."""
    rows = (replace(filters, status="").apply(tender_rows()).order_by()
            .values("eligibility_status").annotate(n=Count("pk")))
    counts = {r["eligibility_status"] or NOT_ASSESSED: r["n"] for r in rows}
    summary = [{"value": value, "label": label, "count": counts.get(value, 0)}
               for value, label in STATUS_LABELS.items()
               if value != NOT_ASSESSED or counts.get(value)]
    return sum(counts.values()), summary


# --- list -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Filters:
    show: str  # "open" or "all"
    status: str
    region: str
    q: str
    scope: str = "it"  # "it" (in scope for an IT firm, the default) or "all"

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
            q=" ".join(without_nul(get.get("q", "")).split())[:MAX_QUERY_CHARS],
            scope="all" if get.get("scope") == "all" else "it",
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
        if self.scope == "it":
            qs = qs.filter(scope_status=IN_SCOPE)
        return qs


@require_safe
def tender_list(request):
    filters = Filters.from_request(request.GET)
    # The scope toggle's two counts, under the same other filters.
    any_scope = replace(filters, scope="all").apply(tender_rows())
    scope_counts = {"it": any_scope.filter(scope_status=IN_SCOPE).count(),
                    "all": any_scope.count()}
    page = Paginator(filters.apply(tender_rows()), PAGE_SIZE).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list)
    for row in page.object_list:
        row.first_blocker = blocker_label(row)
        row.open_reason = open_reason(row)
    status_total, statuses = status_summary(filters)
    return render(request, "tenders/list.html", {
        "status_total": status_total,
        "status_summary": statuses,
        "page": page,
        "filters": filters,
        "status_choices": list(STATUS_LABELS.items()),
        "region_choices": Tender.RegionScope.choices,
        "filtered": bool(filters.status or filters.region or filters.q),
        "scope_counts": scope_counts,
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


def gate_flags(gate: str, flags: list[str], with_documents: bool = False) -> list[str]:
    """The post-validation flags about one gate, in words. ``with_documents``: the model
    also read solicitation documents (prompt v4), so a quote may name one as its source."""
    searched = "the notice or its documents" if with_documents else "the notice"
    out = []
    for flag in flags:
        kind, _, where = flag.partition(":")
        if kind == "ungrounded" and where == f"eligibility.{gate}":
            out.append(f"quote not found in {'its source' if with_documents else searched}: "
                       "treated as unknown")
        elif kind == "source_corrected" and where == f"eligibility.{gate}":
            out.append("quote found in a different source than the model named: source "
                       "corrected")
        elif kind == "ungrounded_detail" and where.startswith(f"eligibility.{gate}."):
            field = where.rsplit(".", 1)[1]
            out.append(f"{_DETAIL_FIELDS.get(field, field)} not found in {searched}: dropped")
    if gate == "supply_arrangement_required" and "sa_metadata_disagrees" in flags:
        out.append("disagrees with the notice metadata, which requires an arrangement")
    return out


def _eligibility(extraction: Extraction | None, source: str) -> dict:
    data = getattr(extraction, source, None) if extraction else None
    return (data or {}).get("eligibility") or {}


REVIEWED = {
    ReviewDecision.Action.CORRECT: "Reviewed by a person",
    ReviewDecision.Action.REJECT: "Reviewed by a person: the model's answer was marked wrong",
}


@dataclass(frozen=True)
class DocumentSource:
    """A solicitation document a quote came from: its name and CanadaBuys link (None when
    the stored URL isn't an allowed CanadaBuys document URL). Never its text. ``uploaded``:
    a reviewer uploaded it (never linked: there is nothing public to link to)."""

    name: str
    url: str | None
    uploaded: bool = False


def document_sources(llm: Extraction | None) -> dict[str, DocumentSource]:
    """Name -> DocumentSource for the documents the model was given (prompt v4)."""
    out = {}
    for d in (llm.documents if llm else None) or []:
        if d.get("status") != "not_read":
            if d.get("source") == "upload":
                out[d["name"]] = DocumentSource(d["name"], None, uploaded=True)
                continue
            url = d.get("url") or ""
            out[d["name"]] = DocumentSource(d["name"], url if is_allowed_url(url) else None)
    return out


def quote_source(g: dict | None, sources: dict[str, DocumentSource]) -> DocumentSource | None:
    """The document a gate's quote came from; None for the notice or no quote. A
    reviewer's correction names its source as the model's quotes do (tenders.review)."""
    if not g or not g.get("evidence"):
        return None
    name = g.get("source")
    if not name or name == NOTICE_SOURCE:
        return None
    return sources.get(name)


@dataclass(frozen=True)
class GateRow:
    gate: str
    label: str
    value: str
    raw_value: str  # what the model said: when it differs, and always on a reviewed row
    evidence: str | None
    flags: list[str]
    baseline: str
    reviewed: str = ""  # set when a person corrected or rejected the model's answer
    document: DocumentSource | None = None  # the quote is from this solicitation document
    version: str = ""  # which extraction the answer came from, when two were combined


VERSION_LABELS = {**{v: f"{v} (notice and documents)" for v in DOCUMENTS_PROMPTS},
                  V3: "v3 (notice only)"}
_DECIDED = {
    ReviewDecision.Action.ACCEPT: "a person reviewed it and accepted the answer from the documents",
    ReviewDecision.Action.CORRECT: "a person reviewed it and corrected the answer",
    ReviewDecision.Action.REJECT: "a person reviewed it and marked the answer wrong",
}


def versions_note(v3_said: str, decision: ReviewDecision | None) -> str:
    """A gate where v3 and v4 both quoted different answers: in the review queue until a
    decision on it is in effect (own or carried), then what that decision was."""
    if decision is None:
        return f"v3 (notice only) said {v3_said}: versions disagree, in the review queue"
    return f"v3 (notice only) said {v3_said}: versions disagreed; {_DECIDED[decision.action]}"


def gate_rows(llm: Extraction | None, baseline: Extraction | None,
              reviews: dict[str, ReviewDecision] | None = None,
              combined: Combined | None = None,
              partner: Extraction | None = None) -> list[GateRow]:
    """The values shown are the ones the assessment used: post-validated, combined per gate
    with the v3 extraction when there is one (``combined``, ``partner``), then reviewed."""
    reviews = reviews or {}
    combined = combined or Combined(llm.data if llm else None)
    reviewed = apply_reviews(combined.data, reviews) if llm and combined.data else None
    checked = (reviewed or {}).get("eligibility") or {}
    raw, raw_v3 = _eligibility(llm, "raw_output"), _eligibility(partner, "raw_output")
    v3_checked = _eligibility(partner, "data")
    base = _eligibility(baseline, "data")
    flags = list(llm.flags) if llm else []
    sources = document_sources(llm)
    rows = []
    for gate in GATES:
        version = combined.versions.get(gate, "")
        from_v3 = version == V3
        value = gate_value(gate, checked.get(gate))
        said = gate_value(gate, (raw_v3 if from_v3 else raw).get(gate))
        decision = reviews.get(gate)
        overridden = bool(decision and decision.action in REVIEWED)
        model_flags = gate_flags(gate, flags, bool(sources))
        if from_v3:  # v4's flags explain why v3's answer is shown
            model_flags = [f"v4: {f}" for f in model_flags] + gate_flags(gate, partner.flags)
        disagree = (f"versions_disagree:eligibility.{gate}" in combined.flags
                    and versions_note(gate_value(gate, v3_checked.get(gate)), decision))
        rows.append(GateRow(
            gate=gate, label=GATE_LABELS[gate], value=value,
            # A person's value can match the model's words (e.g. a correct answer whose
            # quote failed grounding), so a reviewed row always says what the model said.
            raw_value=said if overridden or said != value else "",
            evidence=(checked.get(gate) or {}).get("evidence"),
            # The flags are about the model's answer, not the person's value shown here.
            flags=([f"model's answer: {f}" for f in model_flags] if overridden else model_flags)
            + ([disagree] if disagree else []),
            baseline=gate_value(gate, base.get(gate)),
            reviewed=REVIEWED.get(decision.action, "") if decision else "",
            document=quote_source(checked.get(gate), sources),
            version=VERSION_LABELS.get(version, ""),
        ))
    return rows


def not_stated_labels(not_stated: list[str], eligibility: dict) -> list[str]:
    """Readable names for an assessment's not_stated gates. Clearance counts when either
    side is unknown (rules v3), so name the side when the other one is known."""
    labels = []
    for gate in not_stated:
        label = GATE_LABELS.get(gate, gate)
        g = eligibility.get(gate) or {}
        if gate == "clearance" and g:
            unknown = [side for side in ("corporate", "personnel") if g.get(side) == "unknown"]
            if len(unknown) == 1:
                label = f"{label} ({unknown[0]})"
        labels.append(label)
    return labels


_EXTRACTION_FLAGS = {
    "truncated": "The description was too long and was cut before extraction.",
    "documents_cut": "Some solicitation documents were too long to read in full; the "
                     "documents read are listed below.",
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
    """The code-side detector flagged the notice at ingest or a document the model read,
    or the model said the input tries to instruct it. Either way the page shows one
    neutral note; nothing else changes."""
    return (INJECTION_PATTERN in (tender.data_quality or [])
            or bool(llm and "injection_suspected" in (llm.flags or []))
            or any(d.get("injection") and d.get("status") != "not_read"
                   for d in ((llm.documents if llm else None) or [])))


def labelled(findings: list[dict], eligibility: dict | None = None,
             sources: dict[str, DocumentSource] | None = None) -> list[dict]:
    """Assessment findings ({gate, reason, evidence}) with a readable gate label, and the
    solicitation document the evidence came from when it is the gate's document quote."""
    out = []
    for f in findings:
        g = (eligibility or {}).get(f.get("gate")) or {}
        doc = quote_source(g, sources or {})
        out.append({**f, "gate_label": GATE_LABELS.get(f.get("gate"), f.get("gate")),
                    "document": doc if doc and f.get("evidence") == g.get("evidence")
                    else None})
    return out


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
    """The CSV's own notice link, and only an https one (autoescaping doesn't stop a
    javascript: URL). Never a guessed one: a row flagged notice_url_derived holds a URL that
    an old ingest built from the reference number, which often 404s on CanadaBuys. Migration
    0012 clears those; this covers the time between a deploy and that migration."""
    if tender.notice_url_derived:
        return None
    url = (tender.notice_url or "").strip()
    return url if url.lower().startswith("https://") else None


CANADABUYS_SEARCH = "https://canadabuys.canada.ca/en/tender-opportunities"


def search_url(tender: Tender) -> str | None:
    """When the CSV gives no notice link: a CanadaBuys keyword search for the solicitation
    number (the page labels it a search, never the notice). The reference number finds
    nothing there, so no solicitation number means no link."""
    if official_url(tender):
        return None
    number = " ".join((tender.solicitation_number or "").split())
    return f"{CANADABUYS_SEARCH}?{urlencode({'words': number})}" if number else None


@dataclass(frozen=True)
class ScopeCode:
    scheme: str  # "UNSPSC" or "GSIN"
    code: str
    description: str


@dataclass(frozen=True)
class ScopeLine:
    status: str  # "in", "out" or "unknown"
    codes: list[ScopeCode]  # matched codes (in) or the notice's codes (out)
    more: int = 0  # codes not listed


def scope_line(tender: Tender, assessment: Assessment | None) -> ScopeLine | None:
    """The detail page's scope line, from the stored assessment (None if not assessed yet).
    Code titles come from CategoryCode; a matched code without one shows its IT family."""
    stored = (assessment.scope if assessment else None) or {}
    status = stored.get("status")
    if status not in ("in", "out", "unknown"):
        return None
    if status == "in":
        pairs = [(m["scheme"], m["code"], m["family"]) for m in stored.get("matches", [])]
    else:
        pairs = ([("unspsc", c, "") for c in tender.unspsc_codes]
                 + [("gsin", c.upper(), "") for c in tender.gsin_codes])
    shown = pairs[:SCOPE_LIST_MAX]
    titles = {(c.scheme, c.code): c.description for c in CategoryCode.objects.filter(
        code__in={code for _, code, _ in shown})}
    codes = [ScopeCode(scheme.upper(), code, titles.get((scheme, code)) or family)
             for scheme, code, family in shown]
    return ScopeLine(status, codes, len(pairs) - len(shown))


@require_safe
def tender_detail(request, reference: str):
    tender = get_object_or_404(Tender, reference_number=reference_or_404(reference))
    assessment = (primary_assessments().filter(tender=tender)
                  .select_related("extraction").first())
    llm = assessment.extraction if assessment else (
        tender.extractions.filter(model_id=settings.LLM_MODEL)
        .order_by("-last_used_at", "-id").first())
    baseline = (tender.extractions
                .filter(model_id=BASELINE_MODEL_ID, status=Extraction.Status.OK)
                .order_by("-last_used_at", "-id").first())
    # Only decisions in effect: own ones, and carried ones that still hold.
    reviews = reviews_for(llm).applied if llm else {}
    combined, partner = combined_for(llm, assessment.combined if assessment else None)
    eligibility = ((apply_reviews(combined.data, reviews) or {}).get("eligibility") or {}
                   if llm else {})
    sources = document_sources(llm)
    return render(request, "tenders/detail.html", {
        "tender": tender,
        "published": published_date(tender),
        "official_url": official_url(tender),
        "search_url": search_url(tender),
        "is_open": tender_is_open(tender),
        "assessment": assessment,
        "blockers": labelled(assessment.blockers if assessment else [], eligibility, sources),
        "warnings": labelled(assessment.warnings if assessment else [], eligibility, sources),
        "notes": labelled(assessment.notes if assessment else [], eligibility, sources),
        "not_stated": not_stated_labels(assessment.not_stated if assessment else [],
                                        eligibility),
        "documents_read": [d for d in sources.values() if not d.uploaded],
        "documents_uploaded": [d for d in sources.values() if d.uploaded],
        "llm": llm,
        "baseline": baseline,
        "rows": gate_rows(llm, baseline, reviews, combined, partner),
        "scope": scope_line(tender, assessment),
        "combined_from_v3": sum(v == V3 for v in combined.versions.values()),
        "versions_shown": bool(combined.versions),
        "reviewed_count": sum(d.action != ReviewDecision.Action.ACCEPT for d in reviews.values()),
        "other_flags": extraction_flags(llm.flags if llm else []),
        "injection_caution": INJECTION_CAUTION if injection_caution(tender, llm) else "",
    })
