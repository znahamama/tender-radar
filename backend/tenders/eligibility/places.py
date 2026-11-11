"""Where is a free-text work place: the St. John's area, elsewhere in NL, or outside NL?

Matching is case- and punctuation-insensitive ("St. John's" == "st johns s"), on whole words.
Two-letter province codes only count after a comma ("Ottawa, ON"), so the word "on" in
"office on Water Street" is never read as Ontario.
"""

from __future__ import annotations

import re
from enum import StrEnum

from tenders.extraction.validate import normalize, normalize_loose


class Where(StrEnum):
    ST_JOHNS_AREA = "st_johns_area"
    NL_OTHER = "nl_other"
    OUTSIDE_NL = "outside_nl"
    MIXED = "mixed"  # names places both in and outside NL
    UNKNOWN = "unknown"


ST_JOHNS_AREA = (
    "st john s", "st johns", "saint john s", "mount pearl", "paradise", "conception bay south",
    "cbs", "torbay", "portugal cove", "portugal cove st philip s",
)
NL_MARKERS = ("newfoundland", "labrador", "nl", "nfld", "n l")
NL_TOWNS = (
    "gander", "corner brook", "grand falls windsor", "grand falls", "happy valley goose bay",
    "goose bay", "labrador city", "wabush", "stephenville", "clarenville", "marystown",
    "carbonear", "bay roberts", "harbour grace", "deer lake", "bonavista", "placentia",
    "argentia", "botwood", "lewisporte", "channel port aux basques", "port aux basques",
    "burin", "grand bank", "twillingate", "st anthony", "nain", "churchill falls", "hopedale",
    "makkovik", "rigolet", "postville", "natuashish", "sheshatshiu", "holyrood", "bell island",
    "whitbourne", "trepassey", "ferryland", "bishop s falls", "springdale", "baie verte",
    "rocky harbour", "port saunders", "fogo", "glovertown", "come by chance", "arnold s cove",
)
OUTSIDE_REGIONS = (
    "ontario", "quebec", "nova scotia", "new brunswick", "prince edward island", "manitoba",
    "saskatchewan", "alberta", "british columbia", "yukon", "northwest territories", "nunavut",
    "national capital region", "ncr",
)
OUTSIDE_CITIES = (
    "ottawa", "gatineau", "toronto", "montreal", "halifax",
    "dartmouth", "moncton", "fredericton", "charlottetown", "winnipeg", "regina", "saskatoon",
    "calgary", "edmonton", "vancouver", "victoria", "whitehorse", "yellowknife", "iqaluit",
    "kingston", "hamilton", "sydney", "truro", "quebec city", "sherbrooke", "laval",
)
PROVINCE_CODES = ("on", "qc", "ns", "nb", "pe", "pei", "mb", "sk", "ab", "bc", "yt", "nt", "nu")


def _phrases(words: tuple[str, ...]) -> re.Pattern[str]:
    alts = sorted(words, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in alts) + r")\b")


_AREA = _phrases(ST_JOHNS_AREA)
_NL = _phrases(NL_MARKERS + NL_TOWNS)
_OUTSIDE = _phrases(OUTSIDE_REGIONS + OUTSIDE_CITIES)
_SAINT_JOHN_NB = re.compile(r"\bsaint john\b(?! s\b)|\bst john (?:nb|new brunswick)\b")
_CODE = re.compile(r",\s*(?:" + "|".join(PROVINCE_CODES) + r")\b\.?")


def classify(place: str) -> Where:
    loose = normalize_loose(place)
    area = bool(_AREA.search(loose))
    nl = area or bool(_NL.search(loose))
    outside = bool(_OUTSIDE.search(loose) or _SAINT_JOHN_NB.search(loose)
                   or _CODE.search(normalize(place)))
    if outside and nl:
        return Where.MIXED
    if outside:
        return Where.OUTSIDE_NL
    if area:
        return Where.ST_JOHNS_AREA
    if nl:
        return Where.NL_OTHER
    return Where.UNKNOWN


def _original_pattern(phrase: str) -> str:
    # "st john s" -> r"st[\W_]*john[\W_]*s", run on the original text to keep its spelling.
    return r"[\W_]*".join(re.escape(w) for w in phrase.split())


_FIND = re.compile(
    r"\b(?:" + "|".join(_original_pattern(p) for p in sorted(
        ST_JOHNS_AREA + NL_TOWNS + OUTSIDE_CITIES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def find_place(text: str) -> str | None:
    """First known town or city in ``text``, spelled as it is there (no province names)."""
    m = _FIND.search(text)
    return m.group(0) if m else None
