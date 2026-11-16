"""Select the 30 eval cases and write their label templates.

    source .venv/bin/activate && python eval/select_cases.py

Reads Tender rows only (metadata and redacted text); never Extraction or Assessment rows, so
no model output can reach the label files. Strata and quotas: tenders/evals/selection.py.

Re-runnable: with the same pool (the manifest records its fingerprint) and seed it picks the
same cases. If eval/cases/manifest.json exists, the run only checks that the selection still
matches and writes any missing label file; it never overwrites a manifest that differs or a
label file that exists. Delete them yourself to re-select.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "cases"
MANIFEST = CASES / "manifest.json"

SELECTED_ON = "2026-11-16"
# Only notices first seen before this, so later daily ingests don't change the pool.
FIRST_SEEN_BEFORE = datetime(2026, 11, 17, tzinfo=UTC)
EXCLUDED = {"cb-8448-42897985": "model outputs already seen before labelling"}
PROMPT_EXAMPLES = ("Prompt v2's two worked examples are invented text, not real notices, so no "
                   "notice is excluded for them.")


def setup_django() -> None:
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def build() -> tuple[dict, dict[str, str]]:
    """Return (manifest, {relative file path: label file text})."""
    from django.conf import settings
    from tenders.evals import selection as sel
    from tenders.evals.labels import fmt, render
    from tenders.models import Tender

    max_chars = settings.LLM_MAX_DESCRIPTION_CHARS
    rows = (Tender.objects.filter(first_seen_at__lt=FIRST_SEEN_BEFORE)
            .exclude(reference_number__in=list(EXCLUDED)).order_by("reference_number"))
    pool = {}
    for t in rows:
        desc = t.description or ""
        # Over max_chars the model sees a truncated description; keep both sides identical.
        if len(desc.split()) < sel.MIN_WORDS or len(desc) > max_chars:
            continue
        pool[t.reference_number] = t
    candidates = [
        sel.Candidate(ref, sel.strata_for(
            notice_kind=t.notice_kind, region_scope=t.region_scope,
            tender_status=t.tender_status, title=t.title, description=t.description))
        for ref, t in pool.items()]
    picked = sel.select(candidates)
    dev, test = sel.split(picked)

    fingerprint = hashlib.sha256("\n".join(
        f"{ref}:{t.content_hash}" for ref, t in sorted(pool.items())).encode()).hexdigest()
    cases, files = [], {}
    for split_name, group in (("dev", dev), ("test", test)):
        for i, p in enumerate(group, 1):
            case_id = f"{split_name}-{i:02d}"
            rel = f"{split_name}/{case_id}.yaml"
            t = pool[p.candidate.reference]
            cases.append({
                "case": case_id, "split": split_name, "file": rel,
                "reference": t.reference_number, "content_hash": t.content_hash,
                "picked_for": p.picked_for, "strata": sorted(p.candidate.strata),
            })
            meta = {
                "reference": t.reference_number, "title": t.title, "buyer": t.buyer_org_name,
                "notice_type": t.notice_type,
                "procurement_categories": fmt(t.procurement_categories),
                "regions_of_delivery": fmt(t.regions_of_delivery),
                "regions_of_opportunity": fmt(t.regions_of_opportunity),
                "trade_agreements": fmt(t.trade_agreements),
                "closing_date": fmt(t.closing_date), "url": t.notice_url,
            }
            files[rel] = render(case_id, meta, t.description)

    manifest = {
        "seed": sel.SEED,
        "selected_on": SELECTED_ON,
        "selection_script": "eval/select_cases.py",
        "pool": {
            "rule": (f"kept Tender rows (any status) first seen before "
                     f"{FIRST_SEEN_BEFORE.isoformat()}, redacted description of at least "
                     f"{sel.MIN_WORDS} words and at most {max_chars} characters (no "
                     "truncation), minus the exclusions"),
            "size": len(pool),
            "fingerprint": fingerprint,
        },
        "excluded": EXCLUDED,
        "prompt_examples": PROMPT_EXAMPLES,
        "quotas": sel.QUOTAS,
        "stratum_rules": sel.STRATUM_RULES,
        "strata_counts": {"dev": sel.strata_counts(dev), "test": sel.strata_counts(test),
                          "all": sel.strata_counts(picked)},
        "split": {"dev": [c["reference"] for c in cases if c["split"] == "dev"],
                  "test": [c["reference"] for c in cases if c["split"] == "test"]},
        "cases": cases,
    }
    return manifest, files


def main() -> int:
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    setup_django()
    manifest, files = build()
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    if MANIFEST.exists():
        if MANIFEST.read_text(encoding="utf-8") != text:
            print(f"{MANIFEST.relative_to(ROOT)} differs from today's selection (the pool or "
                  "the code changed). Nothing written. Delete the manifest and label files to "
                  "re-select.", file=sys.stderr)
            return 1
        print("manifest matches the selection")
    else:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(text, encoding="utf-8")
        print(f"wrote {MANIFEST.relative_to(ROOT)}")

    written = 0
    for rel, body in files.items():
        path = CASES / rel
        if path.exists():
            ref = next(c["reference"] for c in manifest["cases"] if c["file"] == rel)
            if f"# reference: {ref}\n" not in path.read_text(encoding="utf-8"):
                print(f"{path.relative_to(ROOT)} exists for a different notice; left alone.",
                      file=sys.stderr)
                return 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written += 1
    print(f"wrote {written} label file(s); {len(files) - written} already existed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
