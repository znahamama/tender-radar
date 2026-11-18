"""Prompt-injection pattern detector: a small, fixed rule set run in code at ingest, before
any LLM call. A hit adds ``injection_pattern`` to the notice's ``data_quality``; the site then
shows a neutral caution on the detail page. It changes no gate, value or status.

It is a tripwire for the obvious forms only (instruction overrides, fake role prefixes, tag
break-outs, text addressed to an AI). It is easy to evade by rewording, and it was written
together with the attacks in eval/injection/, so its hit rate there says little; the useful
number is its false-positive count on real notices (eval/reports/injection-<date>.md).

Pure: no Django, no database. It runs on the redacted text before prompt.neutralize(), so a
literal "</notice>" is seen as written; bracket lookalikes are matched too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

INJECTION_PATTERN = "injection_pattern"  # the data_quality flag

_OPEN = r"[<‹〈⟨＜﹤]"  # < and lookalikes, including the prompt's own neutralized form
_CLOSE = r"[>›〉⟩＞﹥]"
_AI = r"(?:ai|a\.i\.|llm|large language model|language model|chatbot|gpt|claude)"
_TAGS = r"(?:notice|title|metadata|description|system|instructions?|prompt|assistant|user|developer)"

RULES: dict[str, re.Pattern] = {
    # "Ignore previous instructions", "disregard all prior rules", "override the system prompt".
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,30}?"
        r"\b(?:previous|prior|above|earlier|preceding|all|any|your|the system)\b[^.\n]{0,20}?"
        r"\b(?:instructions?|rules|prompts?|guidelines|directives)\b", re.IGNORECASE),
    # A line that starts like a chat turn: "SYSTEM:", "[assistant]:", "<developer>:".
    "role_prefix": re.compile(
        rf"^[ \t>*#\-]*(?:\[|\(|{_OPEN})?\s*(?:system|assistant|developer)\s*"
        rf"(?:\]|\)|{_CLOSE})?\s*(?:message|prompt|note)?\s*:", re.IGNORECASE | re.MULTILINE),
    # Chat-template control tokens.
    "chat_token": re.compile(r"<\|[a-z_]+\|>|\[/?INST\]|<<\s*/?SYS\s*>>", re.IGNORECASE),
    # An opening or closing tag the prompt uses, or a role tag, in < > or a lookalike.
    "tag_lookalike": re.compile(rf"{_OPEN}\s*/?\s*{_TAGS}\b[^<>\n]{{0,40}}?{_CLOSE}", re.IGNORECASE),
    # "You are an AI", "as a language model", "if you are an assistant".
    "you_are_ai": re.compile(
        rf"\b(?:you are|you're|as an?|if you are)\s+(?:now\s+)?(?:an?\s+)?{_AI}\b", re.IGNORECASE),
    # Text addressed to an AI reader: "note to AI assistants", "AI tools summarizing this
    # notice", "any language model reading this".
    "addresses_ai": re.compile(
        rf"\b(?:note|message|instructions?)\s+(?:to|for)\s+(?:any\s+|all\s+)?{_AI}\b"
        rf"|\b{_AI}s?\b(?:\s+\w+){{0,2}}?\s+(?:reading|summari[sz]ing|processing|parsing|"
        rf"extracting|analy[sz]ing|reviewing)\s+(?:this|the)\s+"
        rf"(?:notice|document|tender|text|page|solicitation)", re.IGNORECASE),
    # Output field names from the extraction schema, written in snake_case.
    "schema_field": re.compile(
        r"\b(?:supply_arrangement_required|supplier_origin_restriction|land_claims_applies|"
        r"documents_behind_login|injection_suspected|work_location|criteria_location)\b", re.IGNORECASE),
    # Requests to reveal or repeat the prompt.
    "reveal_prompt": re.compile(
        r"\b(?:reveal|print|repeat|show|output)\b[^.\n]{0,20}?"
        r"\b(?:system prompt|your (?:instructions|prompt|rules))\b", re.IGNORECASE),
}


def scan_text(text: str | None) -> list[str]:
    """Names of the rules that match ``text``, in RULES order."""
    if not text:
        return []
    return [name for name, rx in RULES.items() if rx.search(text)]


def scan(fields: Mapping[str, str | Iterable[str] | None]) -> dict[str, list[str]]:
    """Field name -> rules that matched it (only fields with a hit). List values are joined
    with "; ", as the prompt renders them."""
    hits = {}
    for name, value in fields.items():
        if value is not None and not isinstance(value, str):
            value = "; ".join(str(v) for v in value)
        if rules := scan_text(value):
            hits[name] = rules
    return hits


# The notice fields the model sees: the title, every text metadata value and the description.
SCANNED_FIELDS = ("title", "buyer_org_name", "solicitation_number", "notice_type",
                  "procurement_categories", "regions_of_delivery", "regions_of_opportunity",
                  "trade_agreements", "limited_tendering_reason", "description")


def scan_notice(notice) -> dict[str, list[str]]:
    """``notice``: anything with the SCANNED_FIELDS attributes (TenderNotice or Tender)."""
    return scan({f: getattr(notice, f, None) for f in SCANNED_FIELDS})
