"""Allowlist schema for CanadaBuys tender-notice CSV rows.

Only the fields declared here survive parsing; every other column (contact name, email,
phone, fax, addresses, end-user entities, French duplicates, attachments) is dropped by
``extra="ignore"``. Header names were taken from the real 2026 files (67 columns).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

# CanadaBuys CSVs carry naive timestamps. Spot-checked 2026-11-07: CSV "2026-12-02T16:00:00"
# is shown as "2026/12/02 16:00 EST" on the notice page, so naive values are Eastern Time.
# Note: St. John's is 1.5 h ahead of Eastern, which matters for "closes today" logic.
SOURCE_TZ = ZoneInfo("America/Toronto")


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _split_star_list(v: Any) -> Any:
    """Multi-valued cells look like "*43211500\\n*43233200": newline-separated, '*'-prefixed."""
    if v is None:
        return []
    if isinstance(v, str):
        return [t.strip().lstrip("*").strip() for t in v.splitlines() if t.strip().lstrip("*").strip()]
    return v


def _to_aware_datetime(v: Any) -> Any:
    v = _blank_to_none(v)
    if not isinstance(v, str):
        return v
    try:
        dt = datetime.fromisoformat(v)  # "2026-11-04T09:15:00" or "2026-07-29"
    except ValueError as exc:
        raise ValueError(f"unparseable date {len(v)} chars") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=SOURCE_TZ)


Str = Annotated[str | None, BeforeValidator(_blank_to_none)]
StrList = Annotated[list[str], BeforeValidator(_split_star_list)]
AwareDT = Annotated[datetime | None, BeforeValidator(_to_aware_datetime)]


class TenderNotice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    reference_number: Annotated[str, BeforeValidator(_blank_to_none)] = Field(
        alias="referenceNumber-numeroReference"
    )
    solicitation_number: Str = Field(None, alias="solicitationNumber-numeroSollicitation")
    amendment_number: int | None = Field(None, alias="amendmentNumber-numeroModification")
    amendment_date: AwareDT = Field(None, alias="amendmentDate-dateModification")
    title: Annotated[str, BeforeValidator(_blank_to_none)] = Field(alias="title-titre-eng")
    description: Str = Field(None, alias="tenderDescription-descriptionAppelOffres-eng")
    publication_date: AwareDT = Field(None, alias="publicationDate-datePublication")
    closing_date: AwareDT = Field(None, alias="tenderClosingDate-appelOffresDateCloture")
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

    @field_validator("amendment_number", mode="before")
    @classmethod
    def _amendment(cls, v: Any) -> Any:
        return _blank_to_none(v)  # "003" -> 3 via int coercion; "" -> None

    @property
    def recency_key(self) -> tuple[int, datetime, datetime]:
        """Higher is newer: amendment number, then publication date, then amendment date."""
        floor = datetime.combine(date.min, datetime.min.time(), tzinfo=SOURCE_TZ)
        return (
            self.amendment_number if self.amendment_number is not None else -1,
            self.publication_date or floor,
            self.amendment_date or floor,
        )
