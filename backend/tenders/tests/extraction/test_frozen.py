"""Frozen eval versions (docs/eval-protocol.md). If one of these fails, you changed a frozen
prompt, schema or baseline: revert it, or ship the change as a new version (prompt v3,
rules-v2, ...) and pin that version here, keeping the frozen one for comparison."""

import hashlib
import json

from tenders.extraction.keyword import KEYWORD_RULES_VERSION, KeywordExtractor
from tenders.extraction.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from tenders.extraction.schema import SCHEMA_VERSION, ExtractionData

FROZEN = {
    ("prompt", "v2"): "def2a6603f1cef760a60be80877a6625772f6f2bb8780d5a95344805bc9d3038",
    ("schema", "1"): "4fab443b3ee8a8fe91e28637fb68762324c50751c78531fd4abc57531898e83e",
    ("baseline", "rules-v1"): "04590c85d129cbcff3b584289eac8437938d76677ee1ad58b40444a4a9f76441",
}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_prompt_v2_is_frozen():
    assert PROMPT_VERSION == "v2"
    assert sha256(SYSTEM_PROMPT) == FROZEN[("prompt", PROMPT_VERSION)]


def test_schema_is_frozen():
    schema = json.dumps(ExtractionData.model_json_schema(), sort_keys=True)
    assert SCHEMA_VERSION == "1"
    assert sha256(schema) == FROZEN[("schema", SCHEMA_VERSION)]


def test_keyword_baseline_is_frozen():
    assert KEYWORD_RULES_VERSION == "rules-v1"
    assert KeywordExtractor().fingerprint == FROZEN[("baseline", KEYWORD_RULES_VERSION)]
