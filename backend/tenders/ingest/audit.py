"""Independent redaction audit.

Deliberately does NOT reuse the redactor's patterns. It shares only the rule for which contact
names are scrubbable (``redact._name_variants``: skip single tokens and office/role names).
It reads the contactInfo* columns itself, keeps them in memory for the duration of one audit,
and reports only counts and reference numbers: never matched text. No logging here.
"""

from __future__ import annotations

import csv
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .redact import _name_variants

csv.field_size_limit(min(sys.maxsize, 8 * 1024 * 1024))

_NAME_COL = "contactInfoName-informationsContactNom"
_EMAIL_COL = "contactInfoEmail-informationsContactCourriel"
_PHONE_COLS = ("contactInfoPhone-contactInfoTelephone", "contactInfoFax")
_ID_COLS = ("referenceNumber-numeroReference", "solicitationNumber-numeroSollicitation")

DETECTORS = ("known_name", "known_email", "known_phone", "at_sign", "long_digits", "name_label")
FIELDS = ("description", "title")

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# '@' glued to a word on either side, or "jane @ example.ca" (but not "June 3 @ 2:00PM").
_AT = re.compile(r"\S*(?:\w@|@\w)\S*|\w\s+@\s+[A-Za-z0-9-]+\.[A-Za-z]{2,}")
# 10+ digits, optionally split by single separators, not glued to letters/slashes/#.
_DIGITS = re.compile(r"(?<![\w/#.-])\d(?:[\s.()-]?\d){9,}(?![\w/#-])")
# ...that could be a phone: 10 digits, or 11 starting with 1; not a date; not after an ID word.
_DATE_START = re.compile(r"(?:19|20)\d\d[-./ ]\d\d[-./ ]\d\d")
_ID_CONTEXT = re.compile(
    r"(?:file|no\.?|number|n[o°]|rfq|rfp|rfi|rfso|rfsa|itq|solicitation|nsn|part|event\s+id|"
    r"id|contract|ncage|po|reference|ref\.?|#)\s*[:#]?\s*$",
    re.IGNORECASE,
)
_PHONE_CANDIDATE = re.compile(r"\d[\d\s().+-]{8,24}\d")
_NAME_LABEL = re.compile(
    r"(?<!organization )(?<!organisation )(?<!company )(?<!project )(?<!vessel )"
    r"(?<!Organization )(?<!Organisation )(?<!Company )(?<!Project )(?<!Vessel )"
    # word boundary, or a label glued to the previous word: "...deliveryContracting Authority:"
    r"(?:\b|(?<=[a-z])(?=[A-Z]))"
    r"(?i:(?:name|nom|contact(?:\s+person)?|contracting\s+(?:authority|officer)|"
    r"procurement\s+officer|point\s+of\s+contact|(?:[\w-]+\s+)?poc|attention|attn)\s*[:\-–]\s*)"
    r"[A-ZÀ-Ý][a-zà-ÿ'’-]+(?:[ \t]+[A-ZÀ-Ý][a-zà-ÿ'’-]+)+"
)


def _ten_digits(raw: str | None) -> str | None:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return d if len(d) == 10 else None


@dataclass(repr=False)
class ContactIndex:
    """Every contact name/email/phone in the source CSVs. In memory only; never repr'd."""

    rows: int = 0
    names: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    id_digits: set[str] = field(default_factory=set)  # digit runs of reference/solicitation numbers
    _name_re: re.Pattern[str] | None = None
    _email_re: re.Pattern[str] | None = None

    def __repr__(self) -> str:
        return f"ContactIndex(rows={self.rows}, <redacted>)"

    @classmethod
    def from_csvs(cls, paths: Iterable[Path]) -> ContactIndex:
        idx = cls()
        for path in paths:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    idx.rows += 1
                    idx.names.update(v.casefold() for v in _name_variants(row.get(_NAME_COL)))
                    email = (row.get(_EMAIL_COL) or "").strip().casefold()
                    if "@" in email:
                        idx.emails.add(email)
                    for col in _PHONE_COLS:
                        if p := _ten_digits(row.get(col)):
                            idx.phones.add(p)
                    for col in _ID_COLS:
                        idx.id_digits.update(re.findall(r"\d{6,}", row.get(col) or ""))
        idx._compile()
        return idx

    def _compile(self) -> None:
        def alternation(items: Iterable[str], flex: bool) -> re.Pattern[str] | None:
            parts = [
                r"\s+".join(map(re.escape, s.split())) if flex else re.escape(s)
                for s in sorted(items, key=len, reverse=True)
            ]
            return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)", re.IGNORECASE) if parts else None

        self._name_re = alternation(self.names, flex=True)
        self._email_re = alternation(self.emails, flex=False)

    # --- detectors (each returns a hit count) ----------------------------------------------

    def detect(self, text: str) -> dict[str, int]:
        no_urls = _URL.sub(" ", text)
        return {
            "known_name": len(self._name_re.findall(text)) if self._name_re else 0,
            "known_email": len(self._email_re.findall(text)) if self._email_re else 0,
            "known_phone": self._known_phones(text),  # URLs included on purpose
            "at_sign": len(_AT.findall(no_urls)),
            "long_digits": sum(1 for m in _DIGITS.finditer(no_urls) if self._phone_like(m, no_urls)),
            "name_label": len(_NAME_LABEL.findall(no_urls)),
        }

    def _phone_like(self, m: re.Match[str], text: str) -> bool:
        digits = re.sub(r"\D", "", m.group(0))
        if digits in self.id_digits or _DATE_START.match(m.group(0)):
            return False
        if not (len(digits) == 10 or (len(digits) == 11 and digits[0] == "1")):
            return False
        return not _ID_CONTEXT.search(text[max(0, m.start() - 25) : m.start()])

    def _known_phones(self, text: str) -> int:
        if not self.phones:
            return 0
        hits = 0
        for m in _PHONE_CANDIDATE.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            hits += sum(1 for p in self.phones if p in digits)
        return hits


@dataclass
class AuditReport:
    scanned: int = 0
    hits: dict[tuple[str, str], int] = field(default_factory=dict)
    refs: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def add(self, fld: str, ref: str, found: dict[str, int]) -> None:
        for det, n in found.items():
            if n:
                self.hits[(fld, det)] = self.hits.get((fld, det), 0) + n
                self.refs.setdefault((fld, det), []).append(ref)

    @property
    def total(self) -> int:
        return sum(self.hits.values())


def audit(items: Iterable[tuple[str, str | None, str | None]], index: ContactIndex) -> AuditReport:
    """``items`` are (reference_number, description, title) triples."""
    report = AuditReport()
    for ref, description, title in items:
        report.scanned += 1
        report.add("description", ref, index.detect(description or ""))
        report.add("title", ref, index.detect(title or ""))
    return report
