"""run_eval end to end on one invented case, with the fake LLM and the keyword baseline.
No network: the fake extractor reads a fixture and the real client is blocked."""

import json
from decimal import Decimal

import pytest

from tenders.evals import harness, report
from tenders.evals.labels import BODY, render
from tenders.extraction.budget import CallMeter
from tenders.extraction.keyword import KeywordExtractor
from tenders.extraction.providers import FakeExtractor
from tenders.models import EvalExtraction, Extraction, LlmCall
from tenders.tests.evals.test_labels import DESCRIPTION, FILLED, META
from tenders.tests.extraction.conftest import (  # noqa: F401
    make_tender,
    no_real_api,
    pin_llm_settings,
)


@pytest.fixture
def cases_dir(tmp_path):
    (tmp_path / "dev").mkdir()
    label = render("dev-01", META, DESCRIPTION).split("\n\n" + BODY)[0] + "\n\n" + FILLED
    (tmp_path / "dev" / "dev-01.yaml").write_text(label, encoding="utf-8")
    manifest = {"seed": 1, "selected_on": "2026-11-16", "pool": {"fingerprint": "f"},
                "cases": [{"case": "dev-01", "split": "dev", "file": "dev/dev-01.yaml",
                           "reference": META["reference"], "content_hash": "x",
                           "strata": []}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


class FakeV3(FakeExtractor):
    prompt_version = "v3"


def fakes():
    return {"v2": FakeExtractor(), "v3": FakeV3()}


def test_run_eval_scores_and_stores_repeats_apart(make_tender, cases_dir, tmp_path):  # noqa: F811
    tender = make_tender(reference_number=META["reference"], title=META["title"],
                         description=DESCRIPTION, notice_type=META["notice_type"],
                         notice_kind="against_sa", sa_required_from_metadata=True)
    run = harness.run_eval("dev", 2, prompts=("v2", "v3"), cases_dir=cases_dir, llms=fakes(),
                           baseline=KeywordExtractor(), echo=lambda *_: None)

    # Only the site's prompt (v2) and the baseline get a cached Extraction. v3 never does
    # (the site would show it): its primary run is an EvalExtraction with repeat 0.
    assert Extraction.objects.filter(tender=tender, model_id="fake").count() == 1
    assert Extraction.objects.get(tender=tender, model_id="fake").prompt_version == "v2"
    assert Extraction.objects.filter(tender=tender, model_id="keyword-baseline").count() == 1
    rows = EvalExtraction.objects.filter(run_tag__startswith=run["run_tag"], tender=tender)
    assert rows.count() == 1 + 2 * 2  # v3 primary + 2 repeats for each prompt
    assert rows.get(repeat=0).prompt_version == "v3"
    for p in ("v2", "v3"):  # one tag per prompt keeps (run_tag, tender, repeat) unique
        assert set(rows.filter(prompt_version=p).values_list("run_tag", flat=True)) == {
            harness.eval_tag(run["run_tag"], p)}
    assert not LlmCall.objects.exists()  # the fake extractor makes no API call

    assert run["systems"] == ["llm_v2", "llm_v3", "baseline"]
    case = run["cases"][0]
    assert list(case["systems"]) == run["systems"]
    assert case["systems"]["llm_v3"]["outcome"] == "ok"
    # Labels block (SA required by metadata, profile holds none): expected "blocked".
    assert case["label"]["status"] == "blocked"
    assert case["label"]["fields"]["supply_arrangement_required"] == "yes"
    assert {k: len(v) for k, v in case["repeats"].items()} == {"llm_v2": 2, "llm_v3": 2}

    scored = report.score(run)
    s = scored["scores"]
    assert s["n"] == 1
    for sys in ("llm_v2", "llm_v3"):
        assert s["consistency"][sys]["runs_per_case"] == {"dev-01": 3}
    assert s["changes"]["llm_v3"]["before"] == "llm_v2"
    md, js = report.write(scored, tmp_path / "reports", tag="v3")
    assert md.name.startswith("dev-v3-")
    text = md.read_text()
    for heading in ("## Protocol", "## Summary", "## Error types", "## Status",
                    "## LLM v3 vs LLM v2", "## Consistency", "## Cost, tokens and latency",
                    "## Per-case"):
        assert heading in text
    assert "| Field | LLM v2 | 95% CI | LLM v3 | 95% CI | Baseline |" in text
    assert json.loads(js.read_text())["scores"]["status"]["llm_v3"]["n"] == 1
    md2, _ = report.write(scored, tmp_path / "reports", tag="v3")  # never overwrites
    assert md2 != md and md.exists()

    # A second run reuses the v3 primary run (like the v2 cache) instead of re-rolling it.
    stored = EvalExtraction.objects.count()
    again = harness.run_eval("dev", 0, prompts=("v2", "v3"), cases_dir=cases_dir,
                             llms=fakes(), baseline=KeywordExtractor(), echo=lambda *_: None)
    v3 = again["cases"][0]["systems"]["llm_v3"]
    assert v3["outcome"] == "cache_hit" and v3["eval_extraction_id"] == rows.get(repeat=0).id
    assert EvalExtraction.objects.count() == stored


@pytest.mark.django_db
@pytest.mark.parametrize("prompts, llms, match", [
    (("v9",), None, "not pinned prompt versions"),
    ((), None, "not pinned prompt versions"),
    (("v3",), {"v3": FakeExtractor()}, "has prompt_version v2"),
])
def test_run_eval_refuses_unpinned_or_mismatched_prompts(cases_dir, prompts, llms, match):
    with pytest.raises(harness.EvalError, match=match):
        harness.run_eval("dev", 0, prompts=prompts, cases_dir=cases_dir, llms=llms,
                         baseline=KeywordExtractor(), echo=lambda *_: None)


def test_report_tag_must_be_a_file_name_part(tmp_path):
    for bad in ("../x", "a b", "-v3", "v3/"):
        with pytest.raises(ValueError):
            report.check_tag(bad)
    report.check_tag("v3")
    report.check_tag("v2-v3")


@pytest.mark.django_db
def test_run_eval_refuses_a_missing_tender(cases_dir):
    with pytest.raises(harness.EvalError, match="not in the database"):
        harness.run_eval("dev", 0, cases_dir=cases_dir, llms=fakes(),
                         baseline=KeywordExtractor(), echo=lambda *_: None)


@pytest.mark.django_db
def test_meter_purpose_tags_llm_calls():
    meter = CallMeter(max_calls=5, daily_budget_usd=Decimal(1), purpose="eval")
    meter.record(model_id="claude-haiku-5-5", prompt_version="v2", status="ok")
    assert LlmCall.objects.get().purpose == "eval"
