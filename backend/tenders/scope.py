"""Scope rules: is a notice in scope for a small IT firm? Pure, from category codes only
(no Django, no LLM, never the description). Pinned by hash in tests/extraction/test_frozen.py:
a change ships as a new SCOPE_RULES_VERSION.

Scope rules v1 (SCOPE_RULES_VERSION "1"), 2026-11-19:
- in: at least one UNSPSC or GSIN code in an IT family below (IT services, software,
  hardware, telecom), after the exclusions;
- out: codes present, none in an IT family;
- unknown: no UNSPSC and no GSIN code at all.
One IT code is enough: a notice listing IT among other goods is in scope, so a person
reads it. Known edge cases of that choice (2026-11-19): "Security Equipment" and "Access
Control and Intrusion Detection Services" are in through network security equipment
(43222500), and "Energy Efficiency Regulated Products Testing" through one IT code among 27.
Left out, also known: "Internal Communications System - Zenitel Marine Components" (a
ship's intercom; its codes are in no telecom family). Procurement category (GD/SRV/CNST) and notice type say nothing about IT, so they
are not used.

Families were chosen from the code titles published in the CanadaBuys 2026-2027 CSVs:
- UNSPSC 43: IT, broadcasting and telecommunications (hardware, software, network and
  communications equipment).
- UNSPSC 8111: computer services (programming, systems, data services, software and
  hardware maintenance), except 811119, information retrieval systems and online database
  information retrieval: on the 2026-11-19 data these were purchases of data (satellite
  imagery, space situational awareness data, subscriptions), not IT work.
- UNSPSC 8116: IT service delivery (access management, telecom services, cloud SaaS, PaaS,
  IaaS, equipment as a service), except 811623, cloud-based *business process* as a
  service (payroll, training, HR, procurement processes): outsourced business work.
- UNSPSC 8311: telecommunications media services (telephone, mobile, satellite, fibre,
  enhanced telecom, backbone), except 831118 television and 831119 radio (broadcasting).
- UNSPSC 451119: audio and video conferencing systems (telecom hardware filed under
  audio-visual equipment).
- GSIN D302: informatics professional services. N70: ADP equipment and software.
  JX70: maintenance of ADP equipment and software. WL70: lease of ADP equipment.
- GSIN D304: data transmission. D316: telecommunications network services. D399: other
  telecommunications (voice, satellite).
Left out on purpose: security and surveillance equipment and services (UNSPSC 4617, 9212;
GSIN N5820 CCTV), radio and TV towers (GSIN D309), information products and publications
(GSIN D317, UNSPSC 55), information services (UNSPSC 8312), satellite imaging (UNSPSC
811516), and R&D services (UNSPSC 8114), even when software is part of the work.

Against the ingest filter (tenders.ingest.filters.is_it, unchanged): this adds telecom, and
drops 811119, which the ingest keeps. Everything else the ingest calls IT is in scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

SCOPE_RULES_VERSION = "1"
IN, OUT, UNKNOWN = "in", "out", "unknown"
Status = Literal["in", "out", "unknown"]

# (prefix, family) per scheme; the longest matching prefix decides, so an exclusion
# (family None) inside an IT family wins for that code.
UNSPSC_FAMILIES: tuple[tuple[str, str | None], ...] = (
    ("43", "IT and telecom equipment and software"),
    ("8111", "computer services"),
    ("811119", None),  # information retrieval / online database retrieval (data purchases)
    ("8116", "IT service delivery"),
    ("811623", None),  # cloud-based business process as a service
    ("8311", "telecommunications services"),
    ("831118", None),  # television services
    ("831119", None),  # radio services
    ("451119", "audio and video conferencing systems"),
)
GSIN_FAMILIES: tuple[tuple[str, str | None], ...] = (
    ("D302", "informatics professional services"),
    ("N70", "ADP equipment and software"),
    ("JX70", "ADP maintenance"),
    ("WL70", "ADP lease"),
    ("D304", "data transmission"),
    ("D316", "telecommunications network services"),
    ("D399", "other telecommunications"),
)


@dataclass(frozen=True)
class Scope:
    status: Status
    matches: tuple[tuple[str, str, str], ...] = ()  # (scheme, code, family) that made it in


def _code(raw: str) -> str:
    return raw.strip().lstrip("*").strip().upper()


def family(code: str, families: tuple[tuple[str, str | None], ...]) -> str | None:
    """The IT family of one code, or None (no family, or an exclusion)."""
    best = max((p for p, _ in families if code.startswith(p)), key=len, default=None)
    return dict(families)[best] if best is not None else None


def classify(unspsc_codes: Iterable[str], gsin_codes: Iterable[str] = ()) -> Scope:
    codes = {"unspsc": [c for c in map(_code, unspsc_codes) if c],
             "gsin": [c for c in map(_code, gsin_codes) if c]}
    if not codes["unspsc"] and not codes["gsin"]:
        return Scope(UNKNOWN)
    matches = tuple(
        (scheme, c, f)
        for scheme, families in (("unspsc", UNSPSC_FAMILIES), ("gsin", GSIN_FAMILIES))
        for c in codes[scheme] if (f := family(c, families)) is not None)
    return Scope(IN if matches else OUT, matches)


def notice_scope(notice) -> Scope:
    """For a Tender or a parsed notice (anything with unspsc_codes and gsin_codes)."""
    return classify(notice.unspsc_codes, notice.gsin_codes)


def record(scope: Scope) -> dict:
    """What Assessment.scope stores (JSON)."""
    return {"version": SCOPE_RULES_VERSION, "status": scope.status,
            "matches": [{"scheme": s, "code": c, "family": f} for s, c, f in scope.matches]}
