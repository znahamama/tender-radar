"""Pure relevance filters over parsed notices (no I/O, no Django)."""

from __future__ import annotations

import re
from collections.abc import Iterable

IT_UNSPSC = re.compile(r"^(43\d{6}|8111\d{4})$")

# Region tokens are matched whole (after normalising), never as substrings, so that
# "Saint John" (New Brunswick) can't match "St. John's" (NL).
# Province-level token plus NL places seen in the 2026 data, and other common NL places.
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


def _norm(token: str) -> str:
    t = token.strip().lstrip("*").strip().casefold()
    t = t.replace("’", "'").replace(".", "").replace("'", "")
    return re.sub(r"\s+", " ", t)


def is_nl(regions_of_delivery: Iterable[str]) -> bool:
    return any(_norm(r) in NL_REGIONS for r in regions_of_delivery)


def is_it(unspsc_codes: Iterable[str]) -> bool:
    return any(IT_UNSPSC.match(c.strip().lstrip("*")) for c in unspsc_codes)


def is_national(regions_of_delivery: Iterable[str]) -> bool:
    """Delivery lists "Canada" as a whole (possibly alongside specific provinces)."""
    return any(_norm(r) in NATIONAL_REGIONS for r in regions_of_delivery)


def is_kept(notice) -> bool:
    return is_nl(notice.regions_of_delivery) or is_it(notice.unspsc_codes)
