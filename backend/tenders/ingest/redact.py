"""Contact redaction for tender description text.

Pure functions: no I/O, no logging. ``ContactHints`` hold one row's contactInfo* values
for the duration of that row's processing only; they are never stored or logged.

Order (see ``redact``):
  1. exact-match scrub of the row's own contact hints (name variants, email, phone, fax)
  2. regex scrub of any email address / North American phone number
  3. label-led contact blocks ("Contracting Authority: ...", "Email: ...", ...)
URLs are masked during all three steps so they are never altered.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

NAME, EMAIL, PHONE, BLOCK = "name", "email", "phone", "contact_block"
PLACEHOLDER = {NAME: "[NAME]", EMAIL: "[EMAIL]", PHONE: "[PHONE]", BLOCK: "[CONTACT_BLOCK]"}

HINT_HEADERS = {
    "name": "contactInfoName-informationsContactNom",
    "email": "contactInfoEmail-informationsContactCourriel",
    "phone": "contactInfoPhone-contactInfoTelephone",
    "fax": "contactInfoFax",
}


@dataclass(frozen=True, slots=True, repr=False)
class ContactHints:
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    fax: str | None = None

    def __repr__(self) -> str:  # never leak values into logs or tracebacks
        return "ContactHints(<redacted>)"

    __str__ = __repr__

    @classmethod
    def from_row(cls, row: Mapping[str, str | None]) -> ContactHints:
        return cls(**{k: (row.get(h) or "").strip() or None for k, h in HINT_HEADERS.items()})


# --- patterns -----------------------------------------------------------------------------

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)

EMAIL_RE = re.compile(
    r"(?<![\w.+'-])[\w.%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
    # light de-obfuscation: "jane.doe [at] example [dot] ca"
    r"|(?<![\w.])[\w.%+-]+\s*[\[(]\s*at\s*[\])]\s*[A-Za-z0-9-]+"
    r"(?:\s*(?:[\[(]\s*dot\s*[\])]|\.)\s*[A-Za-z0-9-]+)+",
    re.IGNORECASE,
)

EXT = r"(?:\s*,?\s*(?:ext\.?|extension|x|poste|local)\s*\.?\s*\d{1,6})?"
PHONE_RE = re.compile(
    rf"""
    (?<![\d_$/-])                        # not glued to digits, an amount, a path or ref like W8486-
    (?:(?:\+\s?1|1)[\s.-]?)?              # optional +1 / 1-
    (?:\(\s?\d{{3}}\s?\)\s?|\d{{3}}[\s.-]?)  # area code: (709) or 709 / 709- / 709.
    \d{{3}}[\s.-]\d{{4}}                 # 555-0100 (separator required: protects bare IDs)
    {EXT}                                # optional extension
    (?!\d|[-.]\d)                        # letters may follow: "613-555-0100Inquiries" (lost newline)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Labels that start a contact block. Most must be followed by ":" / "-" (or end the line as
# a heading); "Name"/"Nom" must have a colon; "Inquiries"/"Enquiries" only as bare headings.
_LABELS = (
    r"contracting\s+authority|contracting\s+officer|procurement\s+officer|point\s+of\s+contact"
    r"|contacts?(?:\s+(?:person|information|info|details))?|telephone|t[ée]l[ée]phone|tel\.?"
    r"|phone|fax|e-?mail|courriel|autorit[ée]\s+contractante"
    r"|(?:[\w-]+\s+)?poc|attn\.?"  # "DND POC: ...", "Attn: ..."
)
# Labels whose value is a single datum (an address/number), not a person or block.
_DATUM_LABEL_RE = re.compile(r"t[ée]l|phone|fax|e-?mail|courriel", re.IGNORECASE)
# A contact label glued mid-line to the previous text by lost newlines:
# "...protocols.Contracting Authority: Jane DoeEmail: ..." (case-sensitive on purpose).
_GLUED_CONTACT_LABEL_RE = re.compile(
    r"(?<=[a-z.;:)\]])(?=(?:Contracting Authority|Contracting Officer|Procurement Officer|"
    r"Point of Contact|Contact Person|Contact|Name|Email|E-mail|E-Mail|Telephone|Phone|Tel|"
    r"Fax|Courriel|POC)\s*[:\-–])"
)
_QUALIFIER = r"(?:\s+(?:number|no\.?|address|adresse|information|info|details|\(s\)))?"
LABEL_RE = re.compile(
    rf"^(?P<head>[ \t]*(?:[-*•▪·]\s*)?"
    rf"(?:(?P<label>{_LABELS})(?![\w]){_QUALIFIER}\s*(?P<sep>[:\-–])?"
    rf"|(?P<name>name|nom)\s*(?P<nsep>:)"
    rf"|(?P<heading>inquiries|enquiries|demandes\s+de\s+renseignements)\s*:?\s*$))"
    rf"(?P<value>.*)$",
    re.IGNORECASE,
)
# Lines that may continue a block even though they look like "Label: value".
CONTINUE_RE = re.compile(r"^\s*(?:address|adresse|title|titre|organi[sz]ation|department)\b", re.IGNORECASE)
OTHER_LABEL_RE = re.compile(r"^\s*[A-Za-zÀ-ÿ][\w /&()'-]{1,40}:\s")
# Next field label inside a single glued line (no newline before it, often no space either).
_GLUE_WORDS = (
    r"title|titre|department|organi[sz]ation|telephone|t[ée]l[ée]phone|phone|tel|fax|"
    r"e-?mail|courriel|address|adresse"
)
_GLUE_WORDS_CAP = (
    r"Title|Titre|Department|Organi[sz]ation|Telephone|T[ée]l[ée]phone|Phone|Tel|Fax|"
    r"E-?[Mm]ail|Courriel|Address|Adresse"
)
# Either a standalone word ("... Doe  Title:") or camel-case glue ("DoeTitle:"), never a word
# ending like "Hotel:".
GLUED_LABEL_RE = re.compile(
    rf"(?:(?<![A-Za-zÀ-ÿ])(?i:{_GLUE_WORDS})|(?<=[a-zà-ÿ])(?:{_GLUE_WORDS_CAP}))"
    r"(?:\s+(?i:number|no\.?))?\s*:"
)
# Contact "names" that are really offices/roles ("Contract Administrator", "Department of
# National Defence", "DSG Bids"): scrubbing them would mangle ordinary text, protect nobody.
GENERIC_NAME_RE = re.compile(
    r"\b(?:authority|contracting|contracts?|procurement|services?|team|office|hub|supply|"
    r"division|unit|branch|purchasing|bids?|receiving|canada|group|section|desk|centre|"
    r"center|management|materiel|logistics|finance|administrat\w*|department|directorate|"
    r"general|inbox|mailbox|tenders?|officer|specialist|secretariat|solutions)\b",
    re.IGNORECASE,
)
MAX_BLOCK_LINES = 4
MAX_BLOCK_LINE_LEN = 100  # longer lines are prose, not address/contact lines
MAX_BLOCK_LINE_WORDS = 10  # "Inquiries regarding this requirement must be ..." is prose too
_PLACEHOLDER_RE = re.compile(r"\[(?:NAME|EMAIL|PHONE|CONTACT_BLOCK)\]")


# --- helpers ------------------------------------------------------------------------------

def _name_variants(name: str | None) -> list[str]:
    """"Doe, Jane (SPAC/PSPC) (elle-la / she-her)" -> ["Doe, Jane", "Jane Doe"], etc."""
    if not name:
        return []
    core = re.sub(r"\s+", " ", name.split("(", 1)[0]).strip(" ,/-")
    if not core or "/" in core or "|" in core or GENERIC_NAME_RE.search(core):
        return []  # bilingual office names / roles, not a person
    if "," in core:
        last, first = (p.strip() for p in core.split(",", 1))
    else:
        parts = core.split(" ")
        if len(parts) < 2:
            return []  # single tokens ("Procurement", "Jane") are too ambiguous to scrub
        first, last = " ".join(parts[:-1]), parts[-1]
    if not first or not last:
        return []
    variants = {f"{first} {last}", f"{last}, {first}", f"{last},{first}"}
    return sorted(variants, key=len, reverse=True)


def _flex(literal: str) -> str:
    return r"\s+".join(re.escape(t) for t in literal.split())


def _phone_hint_re(phone: str | None) -> re.Pattern[str] | None:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return None
    a, b, c = digits[:3], digits[3:6], digits[6:]
    sep = r"[\s.-]*"
    return re.compile(
        rf"(?<!\d)(?:(?:\+\s?1|1){sep})?\(?{a}\)?{sep}{b}{sep}{c}(?!\d){EXT}", re.IGNORECASE
    )


def _sub(pattern: re.Pattern[str], kind: str, text: str, counts: dict[str, int]) -> str:
    text, n = pattern.subn(PLACEHOLDER[kind], text)
    counts[kind] += n
    return text


def _scrub_hints(text: str, hints: ContactHints, counts: dict[str, int]) -> str:
    for variant in _name_variants(hints.name):
        text = _sub(re.compile(rf"(?<!\w){_flex(variant)}(?!\w)", re.IGNORECASE), NAME, text, counts)
    if hints.email and "@" in hints.email:
        text = _sub(re.compile(re.escape(hints.email), re.IGNORECASE), EMAIL, text, counts)
    for p in (hints.phone, hints.fax):
        if rx := _phone_hint_re(p):
            text = _sub(rx, PHONE, text, counts)
    return text


def _is_block_continuation(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > MAX_BLOCK_LINE_LEN:
        return False  # blank line ends the block; long lines are prose
    if LABEL_RE.match(line):
        return False  # next contact label starts its own block
    if OTHER_LABEL_RE.match(line) and not CONTINUE_RE.match(line):
        return False  # e.g. "Closing date: ..." is not part of the contact block
    words = _PLACEHOLDER_RE.sub(" ", text).split()
    return len(words) <= MAX_BLOCK_LINE_WORDS


def _has_new_content(text: str) -> bool:
    return bool(re.sub(r"[\W_]+", "", _PLACEHOLDER_RE.sub("", text)))


def _scrub_blocks(text: str, counts: dict[str, int]) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = LABEL_RE.match(lines[i])
        is_block = bool(m) and (
            m.group("heading")
            or m.group("name")
            or m.group("sep")
            or not m.group("value").strip()  # bare "Contracting Authority" heading
        )
        if is_block and m.group("label") and _DATUM_LABEL_RE.match(m.group("label")) and (
            _PLACEHOLDER_RE.match(m.group("value").strip())
        ):
            # "Email: [EMAIL] Closing- July 3, ..." - the datum is already scrubbed; keep the rest.
            is_block = False
        if is_block:
            # A whole block glued onto one line by lost newlines
            # ("Name:   Jane DoeTitle:   OfficerDepartment: ..."): scrub up to the next label.
            value = m.group("value")
            glued = GLUED_LABEL_RE.search(value)
            head_part = value[: glued.start()] if glued else ""
            if glued and _has_new_content(head_part) and len(head_part.strip()) <= MAX_BLOCK_LINE_LEN:
                out.append(f"{m.group('head').rstrip()} {PLACEHOLDER[BLOCK]} {value[glued.start():]}")
                counts[BLOCK] += 1
                i += 1
                continue
            if len(value.strip()) > MAX_BLOCK_LINE_LEN:
                is_block = False  # "Contact: <long sentence>" is prose, leave it
        if not is_block:
            out.append(lines[i])
            i += 1
            continue
        start = i
        i += 1
        for _ in range(MAX_BLOCK_LINES):
            if i >= len(lines) or not _is_block_continuation(lines[i]):
                break
            i += 1
        scrubbed = " ".join([m.group("value"), *lines[start + 1 : i]])
        if not _has_new_content(scrubbed):
            out.extend(lines[start:i])  # only placeholders/punctuation: nothing new to hide
            continue
        out.append(f"{m.group('head').rstrip()} {PLACEHOLDER[BLOCK]}")
        counts[BLOCK] += 1
    return "\n".join(out)


def _mask_urls(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def keep(m: re.Match[str]) -> str:
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"

    return URL_RE.sub(keep, text), urls


def _unmask_urls(text: str, urls: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], text)


# --- public API ---------------------------------------------------------------------------

def redact(text: str | None, hints: ContactHints | None = None) -> tuple[str, dict[str, int]]:
    """Return (clean_text, counts) with contact details replaced by placeholders."""
    counts = {NAME: 0, EMAIL: 0, PHONE: 0, BLOCK: 0}
    if not text:
        return text or "", counts
    masked, urls = _mask_urls(text.replace("\x00", ""))
    masked = _scrub_hints(masked, hints or ContactHints(), counts)
    masked = _sub(EMAIL_RE, EMAIL, masked, counts)
    masked = _sub(PHONE_RE, PHONE, masked, counts)
    masked = _GLUED_CONTACT_LABEL_RE.sub("\n", masked)
    masked = _scrub_blocks(masked, counts)
    return _unmask_urls(masked, urls), counts


def leftover_check(text: str | None) -> dict[str, int]:
    """Count email/phone pattern hits still present (URLs included: nothing is masked)."""
    text = text or ""
    return {EMAIL: len(EMAIL_RE.findall(text)), PHONE: len(PHONE_RE.findall(text))}
