"""Eval harness (run_eval): make sure each case has its extractions, run the LLM repeats,
and collect everything the report needs. Scoring itself is pure (tenders.evals.scoring).

- LLM systems: one per pinned prompt version (``llm_v2``, ``llm_v3``), same model and settings.
  - The site's prompt (prompt.PROMPT_VERSION): the cached Extraction, extracted through the
    normal path (extract_tender, normal caps) if missing, and assessed with assess(), so the
    status is exactly what the site shows.
  - Any other pinned prompt: its primary run is an EvalExtraction with repeat 0, never an
    Extraction (the site shows the last-used Extraction per model, so a v3 Extraction would
    replace v2 on the site). An earlier ok repeat-0 row with the same config and input is
    reused, like a cache hit. It is evaluated in memory with the same rules.
- Baseline: the cached keyword Extraction, as the site's LLM prompt above.
- Repeats: ``repeats`` more calls per case and LLM system that bypass the cache. Each is
  stored as an EvalExtraction, its LlmCall rows have purpose "eval", and it is
  post-validated and evaluated in memory.
- EvalExtraction rows are tagged ``<run tag>-<prompt version>`` (eval_tag()), so each prompt's
  repeats are unique per (tag, tender, repeat) without a schema change.
- Values are compared after post-validation (what the site would show).
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from tenders.eligibility import places, rules
from tenders.eligibility.rules import Profile, TenderFacts, evaluate
from tenders.eligibility.service import assess, default_profile
from tenders.extraction.budget import CallMeter, StopRun
from tenders.extraction.keyword import KEYWORD_RULES_VERSION, KeywordExtractor
from tenders.extraction.prompt import PROMPT_VERSION, PROMPTS, Notice, build_notice
from tenders.extraction.providers import AnthropicExtractor, ExtractionResult, Extractor
from tenders.extraction.schema import SCHEMA_VERSION, ExtractionData
from tenders.extraction.service import extract_tender, revalidate
from tenders.models import EvalExtraction, Extraction, LlmCall, Tender

from . import scoring

CASES_DIR = settings.BASE_DIR.parent / "eval" / "cases"
REPORTS_DIR = settings.BASE_DIR.parent / "eval" / "reports"

# The frozen configuration (docs/eval-protocol.md). The version names are checked here; the
# content hashes are pinned in tenders/tests/extraction/test_frozen.py and recorded per run.
FROZEN_LLM = {"model_id": "claude-haiku-5-5", "effort": "low", "max_tokens": 2000}
# Prompt versions the eval may score, each as its own system. Pin a new one in test_frozen.py
# before adding it here.
PINNED_PROMPTS = ("v2", "v3")
FROZEN_VERSIONS = {"schema": "1", "baseline": "rules-v1", "eligibility": "3"}
BASELINE = scoring.BASELINE


class EvalError(Exception):
    """The run can't produce a valid result (bad manifest, frozen version changed, ...)."""


def llm_system(prompt_version: str) -> str:
    return f"llm_{prompt_version}"


def eval_tag(run_tag: str, prompt_version: str) -> str:
    """The EvalExtraction.run_tag of one prompt's rows in a run."""
    return f"{run_tag}-{prompt_version}"


def frozen_llm_extractor(prompt_version: str = PROMPT_VERSION) -> AnthropicExtractor:
    return AnthropicExtractor(**FROZEN_LLM, prompt_version=prompt_version)


def versions(prompts: tuple[str, ...] = PINNED_PROMPTS) -> dict:
    bad = [p for p in prompts if p not in PINNED_PROMPTS or p not in PROMPTS]
    if bad or not prompts:
        raise EvalError(f"not pinned prompt versions: {bad or prompts} "
                        f"(pinned: {', '.join(PINNED_PROMPTS)})")
    actual = {"schema": SCHEMA_VERSION, "baseline": KEYWORD_RULES_VERSION,
              "eligibility": rules.RULES_VERSION}
    if actual != FROZEN_VERSIONS:
        raise EvalError(f"not the frozen versions: {actual} (expected {FROZEN_VERSIONS})")
    sha = lambda b: hashlib.sha256(b).hexdigest()
    schema = json.dumps(ExtractionData.model_json_schema(), sort_keys=True).encode()
    rules_src = b"".join(Path(m.__file__).read_bytes() for m in (rules, places))
    return {
        **{f"{k}_version": v for k, v in actual.items()},
        "llm": dict(FROZEN_LLM),
        "prompts": {p: {"sha256": sha(PROMPTS[p].encode()),
                        "site_default": p == PROMPT_VERSION} for p in prompts},
        "schema_sha256": sha(schema),
        "baseline_fingerprint": KeywordExtractor().fingerprint,
        "eligibility_sha256": sha(rules_src),
    }


def load_manifest(cases_dir: Path, split: str) -> tuple[dict, list[dict]]:
    path = cases_dir / "manifest.json"
    if not path.is_file():
        raise EvalError(f"no manifest at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    cases = [c for c in manifest["cases"] if c["split"] == split]
    if not cases:
        raise EvalError(f"no {split!r} cases in the manifest")
    return manifest, cases


def git_state(repo: Path) -> dict:
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                                  timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def _system(x: Extraction, status: str) -> dict:
    data = x.data if x.status == Extraction.Status.OK else None
    return {
        "ok": data is not None, "extraction_id": x.id, "created_at": x.created_at.isoformat(),
        "fields": scoring.system_fields(data),
        "evidence": scoring.evidence_by_field((data or {}).get("eligibility")),
        "status": status, "flags": list(x.flags),
        "error": x.error.splitlines()[0] if x.error else "",
    }


@dataclass
class Run:
    split: str
    repeats: int
    run_tag: str
    started_at: datetime
    meter: CallMeter
    log: list[str] = field(default_factory=list)
    stopped: str = ""


def run_eval(split: str = "dev", repeats: int = 0, *, prompts: tuple[str, ...] = PINNED_PROMPTS,
             cases_dir: Path = CASES_DIR, llms: dict[str, Extractor] | None = None,
             baseline: Extractor | None = None, echo=print) -> dict:
    """Run the eval and return the report data (see tenders.evals.report for the rendering).

    ``llms`` maps a prompt version to its extractor (tests); by default each pinned prompt
    gets the frozen model settings."""
    prompts = tuple(prompts)
    vers = versions(prompts)
    manifest, manifest_cases = load_manifest(cases_dir, split)
    llms = {p: (llms or {}).get(p) or frozen_llm_extractor(p) for p in prompts}
    for p, x in llms.items():
        if x.prompt_version != p:
            raise EvalError(f"extractor for prompt {p} has prompt_version {x.prompt_version}")
    baseline = baseline or KeywordExtractor()
    systems = [*(llm_system(p) for p in prompts), BASELINE]
    profile_model = default_profile()
    profile = Profile.from_model(profile_model)
    started = timezone.now()
    run = Run(split, repeats, f"eval-{split}-{started:%Y%m%dT%H%M%SZ}", started,
              CallMeter(max_calls=settings.LLM_MAX_CALLS_PER_RUN,
                        daily_budget_usd=settings.LLM_DAILY_BUDGET_USD))
    first_call_id = (LlmCall.objects.aggregate(m=Max("id"))["m"] or 0) + 1

    cases, tenders, notices, facts = [], {}, {}, {}
    for mc in manifest_cases:
        label = scoring.load_label(cases_dir / mc["file"])
        tender = Tender.objects.filter(reference_number=mc["reference"]).first()
        if tender is None:
            raise EvalError(f"{mc['case']}: tender {mc['reference']} not in the database")
        tf = TenderFacts(tender.sa_required_from_metadata, tender.notice_type or "",
                         tender.notice_kind or "")
        notice = build_notice(tender)
        site = [*((llm_system(p), x) for p, x in llms.items() if p == PROMPT_VERSION),
                (BASELINE, baseline)]
        sys_out = {}
        for name, extractor in site:
            try:
                outcome = extract_tender(tender, extractor, run.meter)
            except StopRun as e:
                raise EvalError(f"{mc['case']}: {name} extraction stopped: {e}") from e
            a = assess(outcome.extraction, profile_model)
            sys_out[name] = {**_system(outcome.extraction, a.status), "outcome": outcome.status}
        for p, extractor in llms.items():
            if p != PROMPT_VERSION:
                sys_out[llm_system(p)] = _eval_only_primary(run, tender, notice, extractor,
                                                            tf, profile)
        sys_out = {s: sys_out[s] for s in systems}
        echo(f"{mc['case']}  {mc['reference']}  "
             + "  ".join(f"{s} {sys_out[s]['outcome']} {sys_out[s]['status']}"
                         for s in systems))
        cases.append({
            "case": mc["case"], "reference": mc["reference"], "file": mc["file"],
            "strata": mc.get("strata", []),
            "text_changed_since_selection": tender.content_hash != mc["content_hash"],
            "notice_kind": tender.notice_kind,
            "sa_required_from_metadata": tender.sa_required_from_metadata,
            "unsure": label.unsure, "notes": label.notes,
            "label": {"fields": label.fields,
                      "evidence": scoring.evidence_by_field(label.gates),
                      "status": scoring.expected_status(label, tf, profile)},
            "systems": sys_out, "repeats": {llm_system(p): [] for p in prompts},
        })
        tenders[mc["case"]], facts[mc["case"]] = tender, tf
        notices[mc["case"]] = notice

    if repeats:
        _run_repeats(run, cases, tenders, notices, facts, llms, profile, echo)

    calls = LlmCall.objects.filter(id__gte=first_call_id,
                                   tender__in=list(tenders.values())).order_by("id")
    return {
        "split": split, "run_tag": run.run_tag, "date": timezone.localdate(started).isoformat(),
        "started_at": started.isoformat(),
        "finished_at": timezone.now().isoformat(),
        "systems": systems,
        "repeats_requested": repeats, "stopped": run.stopped,
        "protocol": {
            "versions": vers, "seed": manifest["seed"], "selected_on": manifest["selected_on"],
            "manifest_pool_fingerprint": manifest["pool"]["fingerprint"],
            "labels": "hand-labelled blind (no model output seen), checked by check_labels",
            "profile": {"name": profile.name,
                        "supply_arrangements": list(profile.supply_arrangements),
                        "corporate_clearance": profile.corporate_clearance,
                        "personnel_clearance": profile.personnel_clearance},
            "git": git_state(settings.BASE_DIR.parent),
        },
        "cases": cases,
        "calls": [{"purpose": c.purpose, "prompt_version": c.prompt_version,
                   "status": c.status, "input_tokens": c.input_tokens,
                   "output_tokens": c.output_tokens, "cost_usd": str(c.cost_usd),
                   "latency_ms": c.latency_ms, "estimated": c.estimated} for c in calls],
    }


def _store(run: Run, tender: Tender, repeat: int, llm: Extractor, notice: Notice,
           result: ExtractionResult) -> EvalExtraction:
    call_flags = (["truncated"] if notice.truncated else []) + result.flags
    return EvalExtraction.objects.create(
        run_tag=eval_tag(run.run_tag, llm.prompt_version), tender=tender, repeat=repeat,
        model_id=llm.model_id,
        prompt_version=llm.prompt_version, schema_version=SCHEMA_VERSION,
        config_fingerprint=llm.fingerprint, input_hash=notice.content_hash,
        status=result.status,
        raw_output=result.data.model_dump(mode="json") if result.data else None,
        call_flags=call_flags, error=result.error)


def _evaluate(row: EvalExtraction, notice: Notice, tender: Tender, tf: TenderFacts,
              profile: Profile) -> dict:
    """Same post-validation and rules as the site, on an unsaved Extraction (nothing written)."""
    transient = Extraction(tender=tender, status=row.status, raw_output=row.raw_output,
                           call_flags=row.call_flags, input_hash=row.input_hash,
                           flags=row.call_flags, error=row.error)
    checked = revalidate(transient, notice, tender)
    data = checked.data if row.status == Extraction.Status.OK else None
    decision = evaluate(data, checked.flags, tf, profile, row.error)
    return {
        "eval_extraction_id": row.id, "ok": data is not None,
        "fields": scoring.system_fields(data),
        "evidence": scoring.evidence_by_field((data or {}).get("eligibility")),
        "status": decision.status, "flags": checked.flags,
        "error": row.error.splitlines()[0] if row.error else "",
    }


def _eval_only_primary(run: Run, tender: Tender, notice: Notice, llm: Extractor,
                       tf: TenderFacts, profile: Profile) -> dict:
    """Primary run of a prompt the site doesn't use: an EvalExtraction with repeat 0.

    Reuses the earliest ok repeat-0 row for the same model, prompt, config and input (so a
    re-run doesn't re-roll it); otherwise calls the model and stores it under this run."""
    row = (EvalExtraction.objects
           .filter(tender=tender, repeat=0, model_id=llm.model_id,
                   prompt_version=llm.prompt_version, config_fingerprint=llm.fingerprint,
                   input_hash=notice.content_hash, status=Extraction.Status.OK)
           .order_by("created_at", "id").first())
    outcome = "cache_hit"
    if row is None:
        run.meter.purpose = "eval"
        try:
            result = llm.extract(notice, run.meter, tender=tender)
        except StopRun as e:
            raise EvalError(f"{tender.reference_number}: llm_{llm.prompt_version} "
                            f"extraction stopped: {e}") from e
        finally:
            run.meter.purpose = "extract"
        row = _store(run, tender, 0, llm, notice, result)
        outcome = row.status
    return {**_evaluate(row, notice, tender, tf, profile), "outcome": outcome,
            "created_at": row.created_at.isoformat(), "run_tag": row.run_tag}


def _run_repeats(run: Run, cases: list[dict], tenders: dict, notices: dict, facts: dict,
                 llms: dict[str, Extractor], profile: Profile, echo) -> None:
    """Round by round (every case and prompt once, then again), so a cap leaves the cases
    and prompts balanced."""
    run.meter.purpose = "eval"
    for i in range(1, run.repeats + 1):
        for c in cases:
            tender, notice = tenders[c["case"]], notices[c["case"]]
            for p, llm in llms.items():
                try:
                    result = llm.extract(notice, run.meter, tender=tender)
                except StopRun as e:
                    run.stopped = f"repeat {i}, {c['case']}, prompt {p}: {e}"
                    echo(f"stopped: {run.stopped}")
                    return
                row = _store(run, tender, i, llm, notice, result)
                rec = _evaluate(row, notice, tender, facts[c["case"]], profile)
                c["repeats"][llm_system(p)].append({"repeat": i, **rec})
                echo(f"repeat {i}  {c['case']}  {p}  {row.status}  {rec['status']}")


def call_stats(calls: list[dict]) -> dict:
    """Totals per purpose and prompt version: calls, tokens, cost, latency (median, mean, max)."""
    out = {}
    keys = sorted({(c["purpose"], c.get("prompt_version", "")) for c in calls})
    for purpose, prompt in keys:
        cs = [c for c in calls
              if c["purpose"] == purpose and c.get("prompt_version", "") == prompt]
        lat = [c["latency_ms"] for c in cs]
        out[f"{purpose} {prompt}".strip()] = {
            "calls": len(cs), "ok": sum(c["status"] == "ok" for c in cs),
            "estimated": sum(c["estimated"] for c in cs),
            "input_tokens": sum(c["input_tokens"] for c in cs),
            "output_tokens": sum(c["output_tokens"] for c in cs),
            "cost_usd": f"{sum(float(c['cost_usd']) for c in cs):.6f}",
            "latency_ms": {"median": int(statistics.median(lat)), "mean": int(statistics.mean(lat)),
                           "max": max(lat)},
        }
    return out
