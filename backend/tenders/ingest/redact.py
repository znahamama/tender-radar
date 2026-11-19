"""Contact redaction for tender description text.

Pure functions: no I/O, no logging. ``ContactHints`` hold one row's contactInfo* values
for the duration of that row's processing only; they are never stored or logged.

Order (see ``redact``):
  1. exact-match scrub of the row's own contact hints (name variants, email, phone, fax)
  2. regex scrub of any email address / North American phone number
  3. label-led contact blocks ("Contracting Authority: ...", "Email: ...", ...)
  4. a person's name right after a role label ("Technical Authority: Jane Doe"), name only
  5. a name line directly above a job-title line (a signature block), name only
URLs are masked during all five steps so they are never altered.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# Bump whenever redact() can give a different result for the same input. Stored document
# text records the version that cleaned it (TenderDocument.redact_version), and the document
# fetcher re-cleans older ones. Notice descriptions need no record: ingest re-redacts them
# from the CSV every run.
#   1: hints, emails, phones, contact blocks (to 2026-11-19)
#   2: + a person's name after a role or meeting-role label
#   3: + a name line directly above a job-title line (signature block)
REDACT_VERSION = 3

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
# A person's name right after a role label ("Technical Authority: Jane Doe", "À l'attention
# de - Jean-François Tremblay", "Project Authority\nJane Q. Doe", PSPC's "The Technical
# Authority for the Contract is:\nJane Doe", an agenda's "Chair: Jane Doe, ..."). Only the
# name is replaced: the label, an honorific and what follows the name (a title, an office)
# stay. Runs after the block scrub, so it only acts where that left a name (labels it doesn't
# know, or prose-length lines).
_ROLE_LABELS = (
    r"technical\s+authority|contracting\s+authority|project\s+authority|attention|contact"
    r"|autorit[ée]\s+technique|autorit[ée]\s+contractante|responsable\s+de\s+projet"
    r"|[àa]\s+l['’]attention\s+de"
    # meeting roles (industry days, bidders' conferences)
    r"|chair|speaker|presented\s+by|moderator"
    r"|présidente?|président\(e\)|présenté(?:e|\(e\))?\s+par"
    r"|animat(?:eur|rice)(?:\s*/\s*animat(?:eur|rice))?"
)
# "... for the Contract is:" / "... pour le contrat est :" between the label and the colon.
_ROLE_FOR = (
    r"(?:\s+(?:for\s+the\s+(?:contract|standing\s+offers?|supply\s+arrangement|requirement)"
    r"\s+is|pour\s+(?:le\s+contrat|l['’]offre\s+à\s+commandes|l['’]arrangement\s+en\s+"
    r"matière\s+d['’]approvisionnement|le\s+besoin)\s+est))?"
)
_BREAK = r"(?:\n(?:[ \t]*\n)?[ \t]*)"  # a line break, one blank line allowed
_ROLE_SEP = (rf"(?P<punct>[ \t]*:[ \t]*{_BREAK}?"  # "Label: Name", "Label :\n\nName"
             rf"|[ \t]+[-–—][ \t]*{_BREAK}?"         # "Label - Name": a spaced dash only,
             rf"|[-–—][ \t]+{_BREAK}?)"              # never "Contact-Free Delivery"
             rf"|[ \t]*{_BREAK}")                    # "Label\nName": label alone on its line
_HONORIFIC = r"(?:(?:Mr|Mrs|Ms|Miss|Dr|Mme|Mlle|M)\.?[ \t]+)"
_NAME_TOKEN = (r"(?:(?:Mc|Mac|O['’]|D['’]|L['’])?[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*"
               r"|[A-ZÀ-Ý]{2,}(?:-[A-ZÀ-Ý]{2,})*)")  # Jane, Jean-François, O'Brien, DOE
_INITIAL = r"[A-ZÀ-Ý]\."
_PARTICLE = r"(?:de|du|des|da|di|van|von|der|den|le|la)"
ROLE_NAME_RE = re.compile(
    rf"(?<![\w])(?P<label>(?i:(?:{_ROLE_LABELS}){_ROLE_FOR}))(?:{_ROLE_SEP}){_HONORIFIC}?"
    rf"(?P<name>{_NAME_TOKEN}(?:[ \t]+(?:{_PARTICLE}[ \t]+)*(?:{_NAME_TOKEN}|{_INITIAL})){{1,5}})"
    r"(?![\w'’-])"
)
_NAME_WORD_RE = re.compile(rf"{_NAME_TOKEN}|{_INITIAL}|{_PARTICLE}")
# Capitalised words that are not part of a person's name after these labels: offices, roles,
# form labels, instructions ("Attention: All Bidders"). A name stops at the first of these.
_NOT_NAME_WORDS = frozenset("""
    the this these that all any each every please note notes for if in on to of and et or ou
    see refer voir same above below not none tbd tba n/a will shall must your our their
    bidder bidders supplier suppliers vendor vendors offeror offerors contractor contractors
    soumissionnaire soumissionnaires fournisseur fournisseurs entrepreneur entrepreneurs
    name nom title titre telephone téléphone tel phone fax email e-mail courriel address adresse
    information details person required requirement mandatory section part annex appendix
    article clause inquiries questions project projet technical technique contracting
    authority autorité client contact attention responsable senior junior manager director
    directeur directrice chief head lead coordinator advisor adviser analyst engineer
    officer agent specialist representative supervisor
    public works government gouvernement travaux publics shared national defence défense
    ministry ministère minister ministre agency agence program programme programs
    committee council board commission bureau direction région region regional régional
    corporate federal fédéral provincial municipal city ville town county small business
    clearance level
    royal canadian coast guard garde côtière fisheries oceans pêches océans health santé
    environment environnement transport transports parks parcs heritage patrimoine
    navy army air force marine military militaire police
    newfoundland labrador nova scotia brunswick prince edward island quebec québec ontario
    manitoba saskatchewan alberta british columbia yukon nunavut northwest territories
    atlantic atlantique pacific pacifique ottawa gatineau halifax moncton toronto montreal
    montréal winnipeg edmonton vancouver victoria
    """.split())  # noqa: SIM905 - a word list reads better as text
MIN_NAME_WORDS = 2  # "Attention: Jane" alone is too ambiguous to scrub
# What may precede a label that has no ":" or dash: nothing but indentation or a bullet. So
# "...questions through to the Contracting Authority\nSSC Magnet" and "Jane Doe, Technical
# Authority\nCabling Team" are left alone.
_LINE_START_RE = re.compile(r"(?:^|\n)[ \t]*(?:[-*•▪·][ \t]*)?\Z")


def _is_not_name(word: str) -> bool:
    return word.lower() in _NOT_NAME_WORDS or bool(GENERIC_NAME_RE.fullmatch(word))


def _person_name_end(candidate: str) -> int | None:
    """End offset of the person's name that ``candidate`` starts with, or None if it doesn't
    start with one. The name is the words before the first non-name word (office, role,
    place, instruction); it needs 2+ words, and isn't an acronym or acronym-led ("SSC
    Magnet")."""
    keep = []
    for w in _NAME_WORD_RE.finditer(candidate):
        if _is_not_name(w.group(0)):
            break
        keep.append(w)
    while keep and not re.fullmatch(_NAME_TOKEN, keep[-1].group(0)):
        keep.pop()  # never end on a particle or an initial
    tokens = [w.group(0) for w in keep if re.fullmatch(_NAME_TOKEN, w.group(0))]
    if (len(tokens) < MIN_NAME_WORDS
            or all(t.isupper() for t in tokens)  # "NRC IT", "PWGSC ATL": acronyms
            or tokens[0].isupper() and not tokens[-1].isupper()):  # "SSC Magnet": an org
        return None
    return keep[-1].end()


def _scrub_role_names(text: str, counts: dict[str, int]) -> str:
    def repl(m: re.Match[str]) -> str:
        whole, offset = m.group(0), m.start("name") - m.start()
        if m.group("punct") is None and not _LINE_START_RE.search(m.string, 0, m.start()):
            return whole
        if (end := _person_name_end(m.group("name"))) is None:
            return whole
        counts[NAME] += 1
        return whole[:offset] + PLACEHOLDER[NAME] + whole[offset + end:]

    return ROLE_NAME_RE.sub(repl, text)


# A signature block: a line that is only a 2-3 word name, directly above a job-title line
# ("Jane Doe\nSupply Team Leader", "Jean Roy\nGestionnaire de l'approvisionnement"). Same
# guards as above, and the whole line must be the name. The title line starts with a
# capitalised word, never a code: "Clearance Level\nB.12 Network Support Specialist" is a
# table header, not a signature.
_TITLE_FIRST = r"[A-ZÀ-Ý][a-zà-ÿ][\w'’&/-]*"
_TITLE_WORD = rf"(?:{_TITLE_FIRST}|[A-ZÀ-Ý]{{2,}}|of|and|for|de|du|des|d['’][\w-]+|&)"
TITLE_LINE_RE = (
    rf"[ \t]*(?:(?:{_TITLE_FIRST})(?:[ \t,]+{_TITLE_WORD}){{0,5}}[ \t]+)?"
    r"(?:Officer|Leader|Specialist|Manager|Authority)s?\b"
    r"|[ \t]*(?:Agente?|Gestionnaire|Cheffe?)\b"
)
SIGNATURE_RE = re.compile(
    rf"^(?P<lead>[ \t]*{_HONORIFIC}?)"
    rf"(?P<name>{_NAME_TOKEN}(?:[ \t]+(?:{_NAME_TOKEN}|{_INITIAL})){{1,2}})"
    rf"(?P<trail>[ \t]*)\n(?=(?:{TITLE_LINE_RE}))",
    re.MULTILINE,
)


def _scrub_signature_names(text: str, counts: dict[str, int]) -> str:
    def repl(m: re.Match[str]) -> str:
        if _person_name_end(m.group("name")) != len(m.group("name")):
            return m.group(0)  # the whole line must be the name
        counts[NAME] += 1
        return f"{m.group('lead')}{PLACEHOLDER[NAME]}{m.group('trail')}\n"

    return SIGNATURE_RE.sub(repl, text)



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
    masked = _scrub_role_names(masked, counts)
    masked = _scrub_signature_names(masked, counts)
    return _unmask_urls(masked, urls), counts


def leftover_check(text: str | None) -> dict[str, int]:
    """Count email/phone pattern hits still present (URLs included: nothing is masked)."""
    text = text or ""
    return {EMAIL: len(EMAIL_RE.findall(text)), PHONE: len(PHONE_RE.findall(text))}
