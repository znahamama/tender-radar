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


def test_run_eval_scores_and_stores_repeats_apart(make_tender, cases_dir, tmp_path):  # noqa: F811
    tender = make_tender(reference_number=META["reference"], title=META["title"],
                         description=DESCRIPTION, notice_type=META["notice_type"],
                         notice_kind="against_sa", sa_required_from_metadata=True)
    run = harness.run_eval("dev", 2, cases_dir=cases_dir, llm=FakeExtractor(),
                           baseline=KeywordExtractor(), echo=lambda *_: None)

    # One cached extraction per system; the repeats live apart under the run tag.
    assert Extraction.objects.filter(tender=tender, model_id="fake").count() == 1
    assert Extraction.objects.filter(tender=tender, model_id="keyword-baseline").count() == 1
    assert EvalExtraction.objects.filter(run_tag=run["run_tag"], tender=tender).count() == 2
    assert not LlmCall.objects.exists()  # the fake extractor makes no API call

    case = run["cases"][0]
    # Labels block (SA required by metadata, profile holds none): expected "blocked".
    assert case["label"]["status"] == "blocked"
    assert case["label"]["fields"]["supply_arrangement_required"] == "yes"
    assert len(case["repeats"]) == 2

    scored = report.score(run)
    s = scored["scores"]
    assert s["n"] == 1 and s["consistency"]["cases"] == 1
    assert s["consistency"]["runs_per_case"] == {"dev-01": 3}
    md, js = report.write(scored, tmp_path / "reports")
    text = md.read_text()
    for heading in ("## Protocol", "## Summary", "## Error types", "## Status",
                    "## Consistency", "## Cost, tokens and latency", "## Per-case"):
        assert heading in text
    assert json.loads(js.read_text())["scores"]["status"]["llm"]["n"] == 1
    md2, _ = report.write(scored, tmp_path / "reports")  # same day: never overwrites
    assert md2 != md and md.exists()


@pytest.mark.django_db
def test_run_eval_refuses_a_missing_tender(cases_dir):
    with pytest.raises(harness.EvalError, match="not in the database"):
        harness.run_eval("dev", 0, cases_dir=cases_dir, llm=FakeExtractor(),
                         baseline=KeywordExtractor(), echo=lambda *_: None)


@pytest.mark.django_db
def test_meter_purpose_tags_llm_calls():
    meter = CallMeter(max_calls=5, daily_budget_usd=Decimal(1), purpose="eval")
    meter.record(model_id="claude-haiku-5-5", prompt_version="v2", status="ok")
    assert LlmCall.objects.get().purpose == "eval"
