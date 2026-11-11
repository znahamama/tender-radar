"""Keyword baseline: regexes only, no LLM, no cost. Fills only the six eligibility gates.

It reads the same input as the LLM (title, metadata values, description, from Notice.sources),
and its output goes through the same grounding and eligibility rules, so the comparison with
the LLM is like for like. Evidence is the matched sentence (or a window of about 200
characters around the match in a very long sentence), or a whole metadata value.

| Gate | "yes" / a level when | "no" / "none" when | otherwise |
|---|---|---|---|
| supply_arrangement_required | notice_type says "against ... supply arrangement / standing offer"; or text says SA/SO holders, pre-qualified, issued pursuant to an SA; or names TBIPS, TSPS, ProServices, SBIPS, SLSA (name = that program) | notice_type is "Request for Supply Arrangement / Standing Offer" | unknown |
| supplier_origin_restriction | canadian_only: "limited to / only" Canadian goods, suppliers or services; canadian_and_trading_partners: that plus "trading partner", or trade_agreements names an international agreement | - | unknown |
| clearance | corporate: FSC / DOS / facility or organization clearance, at the level named in that sentence (top secret, secret, else reliability); personnel: Reliability status, Secret or Top Secret clearance | none/none: "no (mandatory) security requirement", "security requirement: none", "no security associated" | unknown |
| work_location | on_site yes: on-site, on the premises, at the client's/department's site or office, work performed/carried out at; place = first known town in that sentence | on_site no: work may be done remotely, off-site; or procurement_categories is exactly "GD" (goods) and the text never says "install" | unknown |
| land_claims_applies | a land claims agreement sentence says "subject to", "applies", or ": yes"; or trade_agreements names a Land Claim Agreement | that sentence says ": no", "does not apply", "not subject", "n/a" | unknown |
| documents_behind_login | the text names SAP Ariba, MERX, bids&tenders or Biddingo (portal = that name as written), or says documents need a login/account | - | unknown |

Everything else is empty: summary "", work_type "other", criteria_location
"see_solicitation" (the baseline can't tell), no lists, injection_suspected false.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from tenders.eligibility import places

from .budget import CallMeter
from .prompt import DESCRIPTION, META_PREFIX, TITLE, Notice
from .providers import ExtractionResult
from .schema import ExtractionData

if TYPE_CHECKING:
    from tenders.models import Tender

KEYWORD_RULES_VERSION = "rules-v1"
WINDOW = 100  # characters kept on each side of a match inside a very long sentence
LONG_SENTENCE = 300
I = re.IGNORECASE

SA_AGAINST = re.compile(r"\bagainst (?:an? |the )?(?:supply arrangement|standing offer)", I)
SA_REQUEST = re.compile(r"^request for (?:supply arrangement|standing offer)", I)
SA_HOLDERS = re.compile(
    r"(?:supply arrangement|standing offer) holders?"
    r"|holders? of (?:an? |the )?(?:valid )?(?:\w+ ){0,3}(?:supply arrangement|standing offer)"
    r"|pre-?qualified (?:suppliers|under)"
    r"|issued (?:pursuant to|under|against) .{0,60}?(?:supply arrangement|standing offer)", I)
SA_PROGRAM = re.compile(r"\b(TBIPS|TSPS|ProServices|SBIPS|SLSA)\b", I)

CANADIAN_ONLY = re.compile(
    r"limited to canadian (?:goods|suppliers|services)|open only to canadian suppliers"
    r"|canadian (?:suppliers|goods|services) only", I)
TRADING = re.compile(r"trading partners?|applicable trading", I)
DOMESTIC_AGREEMENTS = re.compile(r"canadian free trade|atlantic procurement|please refer|land claim", I)

SECURITY_NONE = re.compile(
    r"no (?:mandatory )?security requirement|security requirements?:?\s*(?:none|n/?a|not applicable)\b"
    r"|no security (?:is )?associated", I)
CORPORATE = re.compile(r"(?i:facility security clearance|designated organi[sz]ation screening"
                       r"|organi[sz]ation(?:al)? security clearance)"
                       r"|(?<![-\w])(?:FSC|DOS)(?![-\w])")  # acronyms: exact case, not "DOS-free"
PERSONNEL = re.compile(r"reliability (?:status|screening|clearance|level)|\btop secret\b"
                       r"|\bsecret (?:level |security )?clearance\b", I)

ON_SITE = re.compile(
    r"\bon[- ]?site\b|\bon the premises\b"
    r"|at the (?:client|department|government|crown|owner)['’]?s? (?:site|premises|facilit(?:y|ies)|offices?)"
    r"|\b(?:work|services) (?:will|must|shall|is to|are to) be (?:performed|carried out|done|completed) at\b",
    I)
REMOTE = re.compile(r"\b(?:work|services) (?:may|can|will|could) be (?:performed|done|carried out) "
                    r"remotely|\bremote work\b|\bwork remotely\b|\boff[- ]?site\b", I)
INSTALL = re.compile(r"\binstall", I)

LAND_CLAIMS = re.compile(r"land claims? agreements?", I)
LAND_NO = re.compile(r":\s*no\b|does not apply|not subject|\bn/a\b", I)
LAND_YES = re.compile(r"subject to|:\s*yes\b|\bapplies\b|\bapply\b", I)

PORTAL = re.compile(r"\b(SAP Ariba|Ariba|MERX|bids ?(?:&|and) ?tenders|Biddingo)\b", I)
LOGIN = re.compile(r"(?:documents?|solicitation).{0,80}\b(?:logged? ?in|sign(?:ed)? in|account)\b"
                   r"|\b(?:logged? ?in|sign(?:ed)? in|account)\b.{0,80}(?:documents?|solicitation)", I)

# Sentence ends, but not after "St." / "Mt." / "No." or a single initial ("N.S.").
_SPLIT = re.compile(r"(?<!\bSt\.)(?<!\bMt\.)(?<!\bNo\.)(?<!\b[A-Z]\.)(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s for s in (x.strip() for x in _SPLIT.split(text)) if s]


def _quote(sentence: str, m: re.Match) -> str:
    """The sentence, or a window around the match snapped to word boundaries."""
    if len(sentence) <= LONG_SENTENCE:
        return sentence
    start, end = max(0, m.start() - WINDOW), min(len(sentence), m.end() + WINDOW)
    while start > 0 and not sentence[start - 1].isspace():
        start -= 1
    while end < len(sentence) and not sentence[end].isspace():
        end += 1
    return sentence[start:end].strip()


def _find(pattern: re.Pattern, sentences: list[str]) -> tuple[str, re.Match] | None:
    for s in sentences:
        if m := pattern.search(s):
            return s, m
    return None


def _level(text: str) -> str:
    t = text.casefold()
    if "top secret" in t:
        return "top_secret"
    if re.search(r"\bsecret\b", t):
        return "secret"
    return "reliability"


class KeywordExtractor:
    model_id = "keyword-baseline"
    prompt_version = KEYWORD_RULES_VERSION

    def __init__(self):
        # Any edit to the rules or the gazetteer changes the fingerprint (and the cache key).
        source = b"".join(Path(f).read_bytes() for f in (__file__, places.__file__))
        self.fingerprint = hashlib.sha256(source).hexdigest()

    def extract(self, notice: Notice, meter: CallMeter,
                tender: Tender | None = None) -> ExtractionResult:
        """No API call: never touches the meter, so it costs nothing and is never capped."""
        return ExtractionResult.ok(ExtractionData.model_validate(self.gates(notice)))

    def gates(self, notice: Notice) -> dict:
        src = dict(notice.sources)
        meta = {k.removeprefix(META_PREFIX): v for k, v in src.items() if k.startswith(META_PREFIX)}
        text = [src.get(TITLE, ""), *_sentences(src.get(DESCRIPTION, ""))]
        everything = " ".join(text)
        return {
            "summary": "", "work_type": "other", "criteria_location": "see_solicitation",
            "deliverables": [], "procurement_method": None, "deadlines": [],
            "mandatory_requirements": [], "certifications": [],
            "eligibility": {
                "supply_arrangement_required": self._sa(meta, text),
                "supplier_origin_restriction": self._origin(meta, text),
                "clearance": self._clearance(text),
                "work_location": self._work_location(meta, text, everything),
                "land_claims_applies": self._land_claims(meta, text),
                "documents_behind_login": self._documents(text),
            },
            "injection_suspected": False,
        }

    @staticmethod
    def _sa(meta: dict, text: list[str]) -> dict:
        notice_type = meta.get("notice_type", "")
        program = _find(SA_PROGRAM, text)
        name = program[1].group(1) if program else None
        if SA_AGAINST.search(notice_type):
            return {"value": "yes", "name": name, "evidence": notice_type}
        if SA_REQUEST.search(notice_type):
            return {"value": "no", "name": None, "evidence": notice_type}
        if hit := _find(SA_HOLDERS, text) or program:
            return {"value": "yes", "name": name, "evidence": _quote(*hit)}
        return {"value": "unknown", "name": None, "evidence": None}

    @staticmethod
    def _origin(meta: dict, text: list[str]) -> dict:
        if hit := _find(CANADIAN_ONLY, text):
            value = "canadian_and_trading_partners" if TRADING.search(hit[0]) else "canadian_only"
            return {"value": value, "evidence": _quote(*hit)}
        for agreement in meta.get("trade_agreements", "").split("; "):
            if agreement and not DOMESTIC_AGREEMENTS.search(agreement):
                return {"value": "canadian_and_trading_partners", "evidence": agreement}
        return {"value": "unknown", "evidence": None}

    @staticmethod
    def _clearance(text: list[str]) -> dict:
        corp_hit, pers_hit = _find(CORPORATE, text), _find(PERSONNEL, text)
        if corp_hit or pers_hit:
            corporate = _level(corp_hit[0]) if corp_hit else "unknown"
            personnel = _level(pers_hit[1].group(0)) if pers_hit else "unknown"
            return {"corporate": corporate, "personnel": personnel,
                    "evidence": _quote(*(corp_hit or pers_hit))}
        if hit := _find(SECURITY_NONE, text):
            return {"corporate": "none", "personnel": "none", "evidence": _quote(*hit)}
        return {"corporate": "unknown", "personnel": "unknown", "evidence": None}

    @staticmethod
    def _work_location(meta: dict, text: list[str], everything: str) -> dict:
        if hit := _find(ON_SITE, text):
            return {"place": places.find_place(hit[0]), "on_site": "yes",
                    "evidence": _quote(*hit)}
        if hit := _find(REMOTE, text):
            return {"place": None, "on_site": "no", "evidence": _quote(*hit)}
        categories = meta.get("procurement_categories", "")
        if categories == "GD" and not INSTALL.search(everything):
            return {"place": None, "on_site": "no", "evidence": categories}
        return {"place": None, "on_site": "unknown", "evidence": None}

    @staticmethod
    def _land_claims(meta: dict, text: list[str]) -> dict:
        for agreement in meta.get("trade_agreements", "").split("; "):
            if re.search(r"land claim", agreement, I):
                return {"value": "yes", "evidence": agreement}
        for s in text:
            if m := LAND_CLAIMS.search(s):
                if LAND_NO.search(s):
                    return {"value": "no", "evidence": _quote(s, m)}
                if LAND_YES.search(s):
                    return {"value": "yes", "evidence": _quote(s, m)}
        return {"value": "unknown", "evidence": None}

    @staticmethod
    def _documents(text: list[str]) -> dict:
        if hit := _find(PORTAL, text):
            return {"value": "yes", "portal": hit[1].group(1), "evidence": _quote(*hit)}
        if hit := _find(LOGIN, text):
            return {"value": "yes", "portal": None, "evidence": _quote(*hit)}
        return {"value": "unknown", "portal": None, "evidence": None}
