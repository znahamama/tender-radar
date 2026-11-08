"""Allowlist schema for CanadaBuys tender-notice CSV rows.

Only the fields declared here survive parsing; every other column (contact name, email,
phone, fax, addresses, end-user entities, French duplicates, attachments) is dropped by
``extra="ignore"``. Header names were taken from the real 2026 files (67 columns).

The description arrives here already normalised and redacted by the pipeline (see
``redact.py``); this model never sees raw contact columns as fields.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# CanadaBuys CSVs carry naive timestamps. Spot-checked 2026-11-07: CSV "2026-12-02T16:00:00"
# is shown as "2026/12/02 16:00 EST" on the notice page, so naive values are Eastern Time.
# Parsed values are converted to UTC. St. John's is 1.5 h ahead of Eastern.
SOURCE_TZ = ZoneInfo("America/Toronto")
NOTICE_URL_TEMPLATE = "https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/{ref}"
PLACEHOLDER_YEAR = 2099
PLACEHOLDER_HORIZON_YEARS = 3  # closing more than this long after publication

# Data-quality flags
CLOSES_BEFORE_PUBLISHED = "closes_before_published"
NO_PUBLICATION_DATE = "no_publication_date"
NO_CLOSING_DATE = "no_closing_date"
PLACEHOLDER_CLOSING_DATE = "placeholder_closing_date"
MENTIONS_STANDING_OFFER = "mentions_standing_offer"

# A description merely mentioning these does not make the notice an SA/SO (it may say
# "this is not a standing offer"), so it only raises a data-quality flag.
MENTIONS_STANDING_RE = re.compile(
    r"supply\s+arrangements?|standing\s+offers?|ongoing\s+qualification|pre-?\s?qualified",
    re.IGNORECASE,
)


def normalize_text(text: str | None) -> str:
    """Unescape HTML entities (&amp; &nbsp; &#64;...) and normalise newlines to \\n."""
    if not text:
        return ""
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\xa0", " ")


def _is_none_literal(v: str) -> bool:
    return v.casefold() == "none"


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _optional_str(v: Any) -> Any:
    v = _blank_to_none(v)
    return None if isinstance(v, str) and _is_none_literal(v) else v


def _split_star_list(v: Any) -> Any:
    """Multi-valued cells look like "*43211500\\n*43233200": newline-separated, '*'-prefixed."""
    if v is None:
        return []
    if isinstance(v, str):
        tokens = (t.strip().lstrip("*").strip() for t in v.splitlines())
        return [t for t in tokens if t and not _is_none_literal(t)]
    return v


def _to_utc_datetime(v: Any) -> Any:
    v = _blank_to_none(v)
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)  # "2026-11-04T09:15:00" or "2026-07-29"
        except ValueError as exc:
            raise ValueError(f"unparseable date ({len(v)} chars)") from exc
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=SOURCE_TZ)).astimezone(UTC)
    return v


Str = Annotated[str | None, BeforeValidator(_optional_str)]
RequiredStr = Annotated[str, BeforeValidator(_blank_to_none)]
StrList = Annotated[list[str], BeforeValidator(_split_star_list)]
UtcDT = Annotated[datetime | None, BeforeValidator(_to_utc_datetime)]


class TenderNotice(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    reference_number: RequiredStr = Field(alias="referenceNumber-numeroReference")
    solicitation_number: Str = Field(None, alias="solicitationNumber-numeroSollicitation")
    amendment_number: int | None = Field(None, alias="amendmentNumber-numeroModification")
    amendment_date: UtcDT = Field(None, alias="amendmentDate-dateModification")
    title: RequiredStr = Field(alias="title-titre-eng")
    # Redacted by the pipeline before validation.
    description: Str = Field(None, alias="tenderDescription-descriptionAppelOffres-eng")
    publication_date: UtcDT = Field(None, alias="publicationDate-datePublication")
    closing_date: UtcDT = Field(None, alias="tenderClosingDate-appelOffresDateCloture")
    tender_status: Str = Field(None, alias="tenderStatus-appelOffresStatut-eng")
    procurement_categories: StrList = Field(
        default_factory=list, alias="procurementCategory-categorieApprovisionnement"
    )
    notice_type: Str = Field(None, alias="noticeType-avisType-eng")
    unspsc_codes: StrList = Field(default_factory=list, alias="unspsc")
    gsin_codes: StrList = Field(default_factory=list, alias="gsin-nibs")
    regions_of_delivery: StrList = Field(
        default_factory=list, alias="regionsOfDelivery-regionsLivraison-eng"
    )
    regions_of_opportunity: StrList = Field(
        default_factory=list, alias="regionsOfOpportunity-regionAppelOffres-eng"
    )
    trade_agreements: StrList = Field(
        default_factory=list, alias="tradeAgreements-accordsCommerciaux-eng"
    )
    limited_tendering_reason: Str = Field(
        None, alias="limitedTenderingReason-raisonAppelOffresLimite-eng"
    )
    notice_url: Str = Field(None, alias="noticeURL-URLavis-eng")
    # Department/agency name only (e.g. "Shared Services Canada"); never a person.
    buyer_org_name: Str = Field(None, alias="contractingEntityName-nomEntitContractante-eng")

    # Derived (no CSV column).
    notice_url_derived: bool = False
    data_quality: list[str] = Field(default_factory=list)
    redaction_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("amendment_number", mode="before")
    @classmethod
    def _amendment(cls, v: Any) -> Any:
        return _blank_to_none(v)  # "003" -> 3 via int coercion; "" -> None

    @model_validator(mode="after")
    def _derive(self) -> TenderNotice:
        if not self.notice_url:
            self.notice_url = NOTICE_URL_TEMPLATE.format(ref=self.reference_number)
            self.notice_url_derived = True
        self.data_quality = quality_flags(self.publication_date, self.closing_date, self.description)
        return self

    @property
    def recency_key(self) -> tuple[int, datetime, datetime]:
        """Higher is newer: amendment number, then publication date, then amendment date."""
        floor = datetime.combine(date.min, datetime.min.time(), tzinfo=UTC)
        return (
            self.amendment_number if self.amendment_number is not None else -1,
            self.publication_date or floor,
            self.amendment_date or floor,
        )


def _add_years(dt: datetime, years: int) -> datetime:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return dt.replace(year=dt.year + years, day=28)


def is_placeholder_closing(published: datetime | None, closing: datetime | None) -> bool:
    """Deterministic: depends only on the notice's own dates, never on today's date."""
    if closing is None:
        return False
    if closing.year >= PLACEHOLDER_YEAR:
        return True
    return published is not None and closing > _add_years(published, PLACEHOLDER_HORIZON_YEARS)


def quality_flags(
    published: datetime | None, closing: datetime | None, description: str | None = None
) -> list[str]:
    flags = []
    if published is None:
        flags.append(NO_PUBLICATION_DATE)
    if closing is None:
        flags.append(NO_CLOSING_DATE)
    elif published is not None and closing < published:
        # publication is a date (midnight Eastern), so this means "closed on an earlier day".
        flags.append(CLOSES_BEFORE_PUBLISHED)
    if is_placeholder_closing(published, closing):
        flags.append(PLACEHOLDER_CLOSING_DATE)
    if description and MENTIONS_STANDING_RE.search(description):
        flags.append(MENTIONS_STANDING_OFFER)
    return flags
