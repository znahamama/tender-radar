"""Frozen eval versions (docs/eval-protocol.md). If one of these fails, you changed a frozen
prompt, schema, baseline or eligibility rules: revert it, or ship the change as a new version
(prompt v3, rules-v2, eligibility rules 4, ...) and pin that version here, keeping the frozen
one for comparison."""

import hashlib
import json
from pathlib import Path

import anthropic
import pytest

from tenders import scope
from tenders.eligibility import places, rules
from tenders.evals.harness import PINNED_PROMPTS
from tenders.extraction.keyword import KEYWORD_RULES_VERSION, KeywordExtractor
from tenders.extraction.prompt import (
    DOCUMENTS_PROMPT_VERSION,
    PROMPT_SCHEMAS,
    PROMPT_VERSION,
    PROMPTS,
    SYSTEM_PROMPT,
)
from tenders.extraction.schema import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_DOCUMENTS,
    SCHEMAS,
    ExtractionData,
    ExtractionData2,
)

FROZEN = {
    ("prompt", "v2"): "def2a6603f1cef760a60be80877a6625772f6f2bb8780d5a95344805bc9d3038",
    # v3: pinned 2026-11-18, designed on the dev split only, before any test scoring.
    # Re-pinned the same day (before any test scoring) after removing the "Invitation to
    # Qualify" wording, which traced to a test case (docs/eval-protocol.md, change 6).
    ("prompt", "v3"): "12c49a099a676105545196d36881831b0b31e8ec1e02b488900d2ca0bf129039",
    # v4: pinned 2026-11-19. v3 for a notice read with its solicitation documents; gate
    # evidence names its source. Only for tenders with ok documents (not in the eval set).
    # Re-pinned the same day, before any successful call: "every quote names a source"
    # became gate evidence only, to fit schema 2 (below).
    ("prompt", "v4"): "50f72e984c708f4e9f24715d220c2fd6fb7c44ec4b37fd1a1b09b353634a7c15",
    ("schema", "1"): "4fab443b3ee8a8fe91e28637fb68762324c50751c78531fd4abc57531898e83e",
    # Schema 2 (prompt v4): schema 1 plus a source on each gate's evidence. Re-pinned twice
    # on 2026-11-19, before any successful use. First version: nullable sources on every
    # quote, 17 union-typed fields (the API allows 16), every v4 request a 400. Second:
    # plain-string sources on every quote, "The compiled grammar is too large" (400). Now:
    # plain-string sources on the six gates only, which compiles.
    ("schema", "2"): "15cdff96e435116f2a032a8affb2df108c692bb38e5d20bb70a0802d4e7bf287",
    ("baseline", "rules-v1"): "04590c85d129cbcff3b584289eac8437938d76677ee1ad58b40444a4a9f76441",
    # rules.py + places.py source (the gazetteer decides on-site blockers).
    ("eligibility", "3"): "69f130cd4bb6823d0a30e49b570fe5249513c039be1fd683ea0e5721a1f02b63",
    # scope.py source: which notices are in scope for an IT firm (category codes only).
    ("scope", "1"): "8edb35a113f6dc0c385b712faa870fc2340239cf575438abc2a1784d85165703",
}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_prompt_v2_is_frozen():
    assert sha256(PROMPTS["v2"]) == FROZEN[("prompt", "v2")]


def test_prompt_v3_is_frozen():
    assert sha256(PROMPTS["v3"]) == FROZEN[("prompt", "v3")]


def test_prompt_v4_is_frozen():
    assert sha256(PROMPTS["v4"]) == FROZEN[("prompt", "v4")]


def test_every_prompt_is_pinned_and_the_site_uses_v3():
    # A new prompt needs a pin above before it exists; the eval scores only pinned ones.
    assert set(PROMPTS) == {v for kind, v in FROZEN if kind == "prompt"}
    # v4 needs solicitation documents, which the eval's notice-only cases don't have.
    assert set(PINNED_PROMPTS) == set(PROMPTS) - {DOCUMENTS_PROMPT_VERSION}
    # The site switched to v3 on 2026-11-18, after the single test run. v2 stays pinned.
    assert PROMPT_VERSION == "v3" and SYSTEM_PROMPT is PROMPTS["v3"]


def test_schema_is_frozen():
    schema = json.dumps(ExtractionData.model_json_schema(), sort_keys=True)
    assert SCHEMA_VERSION == "1"
    assert sha256(schema) == FROZEN[("schema", SCHEMA_VERSION)]


def test_schema_2_is_frozen_and_only_v4_uses_it():
    schema = json.dumps(ExtractionData2.model_json_schema(), sort_keys=True)
    assert SCHEMA_VERSION_DOCUMENTS == "2"
    assert sha256(schema) == FROZEN[("schema", SCHEMA_VERSION_DOCUMENTS)]
    assert set(SCHEMAS) == {v for kind, v in FROZEN if kind == "schema"}
    assert PROMPT_SCHEMAS == {"v2": "1", "v3": "1", "v4": "2"}
    assert DOCUMENTS_PROMPT_VERSION == "v4"


def test_keyword_baseline_is_frozen():
    assert KEYWORD_RULES_VERSION == "rules-v1"
    assert KeywordExtractor().fingerprint == FROZEN[("baseline", KEYWORD_RULES_VERSION)]


def test_eligibility_rules_v3_are_frozen():
    assert rules.RULES_VERSION == "3"
    source = b"".join(Path(m.__file__).read_bytes() for m in (rules, places))
    assert hashlib.sha256(source).hexdigest() == FROZEN[("eligibility", rules.RULES_VERSION)]


def test_scope_rules_v1_are_frozen():
    assert scope.SCOPE_RULES_VERSION == "1"
    source = Path(scope.__file__).read_bytes()
    assert hashlib.sha256(source).hexdigest() == FROZEN[("scope", scope.SCOPE_RULES_VERSION)]


# Structured outputs' published limits per request (docs: "Schema complexity limits").
# Over either one, every request with that schema is a 400.
MAX_UNION_TYPED = 16  # fields using anyOf or a type array, e.g. ["string", "null"]
MAX_OPTIONAL = 24  # properties not listed in "required"


def _complexity(node) -> tuple[int, int]:
    unions = optional = 0
    if isinstance(node, dict):
        if "anyOf" in node or isinstance(node.get("type"), list):
            unions += 1
        if isinstance(node.get("properties"), dict):
            optional += len(set(node["properties"]) - set(node.get("required", [])))
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return 0, 0
    for child in children:
        u, o = _complexity(child)
        unions, optional = unions + u, optional + o
    return unions, optional


@pytest.mark.parametrize("version", sorted(SCHEMAS))
def test_schema_fits_structured_output_limits(version):
    """Measured on what the SDK sends (transform_schema), as providers.py does."""
    unions, optional = _complexity(anthropic.transform_schema(SCHEMAS[version]))
    assert unions <= MAX_UNION_TYPED, f"schema {version}: {unions} union-typed fields"
    assert optional <= MAX_OPTIONAL, f"schema {version}: {optional} optional fields"


def test_complexity_counter_counts_unions_and_optionals():
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "b": {"type": ["string", "null"]}, "c": {"type": "string"}}}
    assert _complexity(schema) == (2, 2)
