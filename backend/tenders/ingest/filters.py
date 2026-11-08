"""Pure relevance filters and labels over parsed notices (no I/O, no Django)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .schema import PLACEHOLDER_CLOSING_DATE

IT_UNSPSC = re.compile(r"^(43\d{6}|8111\d{4})$")

# GSIN prefixes that mean IT, chosen from the codes present in the 2026 files:
#   D302  Informatics professional & consulting services (software dev, systems/project mgmt)
#   N70   ADP equipment & software (computers, storage, LAN, software): the GSIN twin of UNSPSC 43
#   JX70  Maintenance of ADP equipment & software
#   WL70  Lease of ADP equipment
# Deliberately excluded from the D3 family: D304 data transmission, D309 radio/TV towers,
# D316 telecom network services, D399 other telecom (voice, satellite): carrier/telecom work,
# and D317 information products (publications/subscriptions).
IT_GSIN_PREFIXES = ("D302", "N70", "JX70", "WL70")

# Region tokens are matched whole (after normalising), never as substrings, so that
# "Saint John" (New Brunswick) can't match "St. John's" (NL).
NL_REGIONS = frozenset(
    {
        "newfoundland and labrador",
        "newfoundland",
        "labrador",
        "nl",
        "st johns",
        "mount pearl",
        "paradise",
        "conception bay south",
        "gander",
        "corner brook",
        "grand falls-windsor",
        "stephenville",
        "happy valley-goose bay",
        "goose bay",
        "labrador city",
        "wabush",
        "charlottetown (labrador)",
        "argentia",
        "clarenville",
        "marystown",
        "st anthony",
        "botwood",
        "nain",
    }
)
NATIONAL_REGIONS = frozenset({"canada"})

STANDING_RE = re.compile(
    r"supply\s+arrangements?|standing\s+offers?|ongoing\s+qualification|pre-?\s?qualified",
    re.IGNORECASE,
)

SCOPE_NL, SCOPE_NATIONAL, SCOPE_BOTH = "nl", "national", "nl+national"
KIND_STANDING, KIND_REGULAR = "standing_or_ongoing", "regular"


def _norm(token: str) -> str:
    t = token.strip().lstrip("*").strip().casefold()
    t = t.replace("’", "'").replace(".", "").replace("'", "")
    return re.sub(r"\s+", " ", t)


def is_nl(regions_of_delivery: Iterable[str]) -> bool:
    return any(_norm(r) in NL_REGIONS for r in regions_of_delivery)


def is_national(regions_of_delivery: Iterable[str]) -> bool:
    """Delivery lists "Canada" as a whole (possibly alongside specific provinces)."""
    return any(_norm(r) in NATIONAL_REGIONS for r in regions_of_delivery)


def is_it(unspsc_codes: Iterable[str], gsin_codes: Iterable[str] = ()) -> bool:
    if any(IT_UNSPSC.match(c.strip().lstrip("*")) for c in unspsc_codes):
        return True
    return any(c.strip().lstrip("*").upper().startswith(IT_GSIN_PREFIXES) for c in gsin_codes)


def notice_is_it(notice) -> bool:
    return is_it(notice.unspsc_codes, notice.gsin_codes)


def region_scope(regions_of_delivery: Iterable[str]) -> str | None:
    regions = list(regions_of_delivery)
    nl, national = is_nl(regions), is_national(regions)
    if nl and national:
        return SCOPE_BOTH
    return SCOPE_NL if nl else SCOPE_NATIONAL if national else None


def is_kept(notice) -> bool:
    """NL delivery (any category), or IT that is national/remote-capable or in NL."""
    nl = is_nl(notice.regions_of_delivery)
    return nl or (notice_is_it(notice) and (is_national(notice.regions_of_delivery) or nl))


def is_dropped_regional_it(notice) -> bool:
    """IT delivered only to other regions: the firm works in St. John's or remotely only."""
    return notice_is_it(notice) and region_scope(notice.regions_of_delivery) is None


def notice_kind(notice) -> str:
    text = " ".join(filter(None, (notice.notice_type, notice.title, notice.description)))
    if STANDING_RE.search(text) or PLACEHOLDER_CLOSING_DATE in notice.data_quality:
        return KIND_STANDING
    return KIND_REGULAR
