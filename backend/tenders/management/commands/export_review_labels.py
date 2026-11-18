"""Write corrected extractions as candidate label files in eval/cases/reviewed/.

    python manage.py export_review_labels [--dir eval/cases/reviewed]

A separate pool for future eval data: never listed in eval/cases/manifest.json, so run_eval
and check_labels never score it with dev or test. A notice that is a dev or test case is
skipped, so the pools never share a notice.

Each file has the same comment block and fields as a dev/test label file
(tenders.evals.labels.render), so the same checker reads it. Only a person's corrections
are filled in. Label files hold no model output (docs/eval-protocol.md), so a gate whose
latest decision accepts or rejects the model's answer, or that wasn't reviewed, is left as
TODO for a labeller, with a comment saying which. An existing file is never overwritten.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from tenders.evals.labels import TODO, check_text, fmt, render
from tenders.models import Extraction, ReviewDecision
from tenders.review import CRITERIA, detail_fields, latest_decisions, value_fields

CASES_DIR = settings.BASE_DIR.parent / "eval" / "cases"

GATE_COMMENTS = {  # the allowed-values comments of a dev/test label file (labels.BODY)
    "supply_arrangement_required": "value: yes | no | unknown. name: the arrangement as "
                                   "written in the notice, or null",
    "supplier_origin_restriction": "value: none | canadian_only | "
                                   "canadian_and_trading_partners | unknown",
    "clearance": "corporate, personnel: none | reliability | secret | top_secret | unknown",
    "work_location": "on_site: yes | no | unknown. place: where staff must work "
                     "(title/description), or null",
    "land_claims_applies": "value: yes | no | unknown",
    "documents_behind_login": "value: yes | no | unknown. portal: as written in the notice, "
                              "or null",
}
# Key order inside each gate, as in labels.BODY.
GATE_KEYS = {
    "supply_arrangement_required": ("value", "name"),
    "supplier_origin_restriction": ("value",),
    "clearance": ("corporate", "personnel"),
    "work_location": ("on_site", "place"),
    "land_claims_applies": ("value",),
    "documents_behind_login": ("value", "portal"),
}


def _scalar(v, bare: bool) -> str:
    """Enumerated values bare, as in the dev/test files (labels._value reads yes/no back);
    free text as a JSON string, which is also a valid YAML double-quoted string."""
    if v is None:
        return "null"
    return v if bare else json.dumps(v, ensure_ascii=False)


def _provenance(d: ReviewDecision | None) -> str:
    if d is None:
        return "not reviewed: label it"
    if d.action == ReviewDecision.Action.CORRECT:
        return f"corrected by a person (review decision {d.pk})"
    if d.action == ReviewDecision.Action.ACCEPT:
        return (f"model answer accepted (review decision {d.pk}); label files hold no model "
                "output, so label it")
    return f"model answer rejected (review decision {d.pk}): label it"


def body(decisions: dict[str, ReviewDecision]) -> str:
    def corrected(gate):
        d = decisions.get(gate)
        return d if d and d.action == ReviewDecision.Action.CORRECT else None

    crit = corrected(CRITERIA)
    lines = [f"# {_provenance(decisions.get(CRITERIA))}",
             (f"criteria_location: {crit.corrected_value['value'] if crit else TODO}"
              "   # stated_in_notice | partly_stated | see_solicitation"),
             "gates:"]
    for gate, keys in GATE_KEYS.items():
        assert set(keys) == {*value_fields(gate), *detail_fields(gate)}
        d = corrected(gate)
        values = ({**d.corrected_value, "evidence": d.evidence} if d else
                  {**{k: TODO for k in value_fields(gate)},
                   **{k: None for k in detail_fields(gate)}, "evidence": None})
        inner = ", ".join(f"{k}: {_scalar(values[k], k in value_fields(gate))}"
                          for k in (*keys, "evidence"))
        lines += [f"  # {GATE_COMMENTS[gate]}",
                  f"  # {_provenance(decisions.get(gate))}",
                  f"  {gate}: {{{inner}}}"]
    lines += ["unsure: false   # true only for a genuinely ambiguous case, with a one-line note",
              'notes: ""', ""]
    return "\n".join(lines)


def label_text(extraction: Extraction, case_id: str,
               decisions: dict[str, ReviewDecision]) -> str:
    t = extraction.tender
    meta = {
        "reference": t.reference_number, "title": t.title, "buyer": t.buyer_org_name,
        "notice_type": t.notice_type, "procurement_categories": fmt(t.procurement_categories),
        "regions_of_delivery": fmt(t.regions_of_delivery),
        "regions_of_opportunity": fmt(t.regions_of_opportunity),
        "trade_agreements": fmt(t.trade_agreements), "closing_date": fmt(t.closing_date),
        "url": t.notice_url,
    }
    head = render(case_id, meta, t.description).split("\n\ncriteria_location:")[0]
    return f"{head}\n\n{body(decisions)}"


def safe_name(reference: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", reference).strip("._") or "notice"


class Command(BaseCommand):
    help = "Write corrected extractions as candidate label files (a pool apart from dev/test)."

    def add_arguments(self, parser):
        parser.add_argument("--dir", type=Path, default=CASES_DIR / "reviewed",
                            help="Output folder (default: eval/cases/reviewed).")
        parser.add_argument("--manifest", type=Path, default=CASES_DIR / "manifest.json",
                            help="The dev/test manifest; its notices are never exported.")

    def handle(self, *args, dir: Path, manifest: Path, **options):
        eval_refs = set()
        if manifest.is_file():
            eval_refs = {c["reference"] for c in json.loads(
                manifest.read_text(encoding="utf-8"))["cases"]}
        ids = (ReviewDecision.objects.filter(action=ReviewDecision.Action.CORRECT)
               .values_list("extraction_id", flat=True).distinct())
        extractions = list(Extraction.objects.filter(pk__in=list(ids)).select_related("tender")
                           .prefetch_related("review_decisions")
                           .order_by("tender__reference_number", "id"))
        per_tender = Counter(x.tender_id for x in extractions)
        written = skipped = 0
        for x in extractions:
            decisions = latest_decisions(x)
            if not any(d.action == ReviewDecision.Action.CORRECT for d in decisions.values()):
                continue  # every correction was superseded
            ref = x.tender.reference_number
            if ref in eval_refs:
                self.stdout.write(f"{ref}: a dev/test case, skipped (pools never mix)")
                skipped += 1
                continue
            name = safe_name(ref)
            if per_tender[x.tender_id] > 1:  # corrections on two extractions of one notice
                name = f"{name}-x{x.pk}"
            path = dir / f"{name}.yaml"
            if path.exists():
                self.stdout.write(f"{path.name}: exists, not overwritten (delete it to "
                                  "re-export)")
                skipped += 1
                continue
            text = label_text(x, f"reviewed-{name}", decisions)
            dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written += 1
            report = check_text(path, text)
            self.stdout.write(f"{path.name}: written, {report.todo} TODO left")
            for problem in report.problems:
                self.stdout.write(f"  - {problem}")
        self.stdout.write(f"{written} written, {skipped} skipped")
