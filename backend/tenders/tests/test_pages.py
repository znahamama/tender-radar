"""Public pages: list filters and pagination, detail, escaping, security headers."""

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from tenders.extraction.keyword import KeywordExtractor
from tenders.models import Assessment, Extraction, SupplierProfile, Tender
from tenders.tests.extraction.conftest import valid_data

pytestmark = pytest.mark.django_db

LLM = "claude-haiku-5-5"
SEEN = datetime(2026, 11, 8, 2, 30, tzinfo=UTC)  # 2026-11-07 23:00 NST


@pytest.fixture(autouse=True)
def pin_model(settings):
    settings.LLM_MODEL = LLM


def make_tender(ref, **fields):
    soon = timezone.now() + timedelta(days=10)
    defaults = {
        "title": f"Tender {ref}", "description": "Plain description.",
        "notice_url": f"https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/{ref}",
        "tender_status": "Open", "closing_date": soon, "region_scope": "nl",
        "notice_kind": "regular", "buyer_org_name": "Public Services and Procurement Canada",
        "notice_type": "RFP", "content_hash": "x",
        "first_seen_at": SEEN, "last_seen_at": SEEN, "updated_at": SEEN,
    }
    return Tender.objects.create(reference_number=ref, **{**defaults, **fields})


def assess(tender, status, *, model_id=LLM, blockers=(), warnings=(), not_stated=(),
           data=None, flags=(), created_at=None):
    x = Extraction.objects.create(
        tender=tender, content_hash=f"h-{model_id}-{Extraction.objects.count()}",
        model_id=model_id, prompt_version="v2", schema_version="1", status="ok",
        raw_output=data, data=data, flags=list(flags))
    if created_at:
        Extraction.objects.filter(pk=x.pk).update(created_at=created_at)
    return Assessment.objects.create(
        tender=tender, extraction=x, profile=SupplierProfile.objects.order_by("id").first(),
        status=status, blockers=list(blockers), warnings=list(warnings),
        not_stated=list(not_stated), rules_version="2", inputs_hash="0" * 64,
        computed_at=timezone.now())


def blocker(reason, gate="clearance", evidence=None, label=None):
    f = {"gate": gate, "reason": reason, "evidence": evidence}
    if label is not None:  # findings stored before rules v3 have no label
        f["label"] = label
    return f


def refs(response):
    return [t.reference_number for t in response.context["page"].object_list]


def get_list(client, **params):
    r = client.get(reverse("tender-list"), params)
    assert r.status_code == 200
    return r


# --- list -----------------------------------------------------------------------------

def test_list_shows_open_by_default_and_all_on_toggle(client):
    make_tender("OPEN-1")
    make_tender("OPEN-NO-DATE", closing_date=None)
    make_tender("EXPIRED", tender_status="Expired", closing_date=timezone.now() - timedelta(days=3))
    # Open at the last ingest, but its closing date has passed since.
    make_tender("PAST-CLOSE", closing_date=timezone.now() - timedelta(hours=1))

    assert set(refs(get_list(client))) == {"OPEN-1", "OPEN-NO-DATE"}
    assert set(refs(get_list(client, show="all"))) == {
        "OPEN-1", "OPEN-NO-DATE", "EXPIRED", "PAST-CLOSE"}
    assert set(refs(get_list(client, show="nonsense"))) == {"OPEN-1", "OPEN-NO-DATE"}


def test_list_orders_open_by_closing_date_soonest_first(client):
    now = timezone.now()
    make_tender("LATER", closing_date=now + timedelta(days=20))
    make_tender("SOONER", closing_date=now + timedelta(days=2))
    make_tender("NO-DATE", closing_date=None)
    assert refs(get_list(client)) == ["SOONER", "LATER", "NO-DATE"]


def test_list_status_filter(client):
    assess(make_tender("B"), "blocked", blockers=[blocker("Corporate clearance required.")])
    assess(make_tender("C"), "check_solicitation")
    assess(make_tender("N"), "no_blockers_stated")
    make_tender("UNASSESSED")

    assert refs(get_list(client, status="blocked")) == ["B"]
    assert refs(get_list(client, status="check_solicitation")) == ["C"]
    assert refs(get_list(client, status="no_blockers_stated")) == ["N"]
    assert refs(get_list(client, status="not_assessed")) == ["UNASSESSED"]
    # An unknown value is ignored, not an error.
    assert len(refs(get_list(client, status="bogus"))) == 4


def test_list_uses_newest_llm_assessment_not_the_baseline(client):
    t = make_tender("T")
    old = timezone.now() - timedelta(days=2)
    assess(t, "blocked", blockers=[blocker("Old blocker.")], created_at=old)
    assess(t, "check_solicitation")
    assess(t, "blocked", model_id=KeywordExtractor.model_id,
           blockers=[blocker("Baseline blocker.")])

    r = get_list(client)
    row = r.context["page"].object_list[0]
    assert (row.eligibility_status, row.first_blocker) == ("check_solicitation", "")
    assert refs(get_list(client, status="blocked")) == []
    assert b"Old blocker" not in r.content and b"Baseline blocker" not in r.content


def test_list_shows_first_blocker_label_and_badge(client):
    t = make_tender("B")
    assess(t, "blocked", blockers=[
        blocker("Bidding is limited to holders of the E60HN supply arrangement; the profile "
                "holds none of it.", gate="supply_arrangement_required",
                label="Supply arrangement (E60HN)"),
        blocker("Corporate clearance required: Secret (profile: none).",
                label="Clearance: Secret (corporate)"),
    ])
    html = get_list(client).content.decode()
    assert ">Supply arrangement (E60HN)</td>" in html
    assert "Bidding is limited" not in html and "Clearance: Secret" not in html
    assert 'class="badge badge-blocked">Blocked<' in html
    # The full sentence stays on the detail page.
    detail = client.get(reverse("tender-detail", args=["B"])).content.decode()
    assert ("Bidding is limited to holders of the E60HN supply arrangement; the profile "
            "holds none of it.") in detail


@pytest.mark.parametrize(("finding", "shown"), [
    (blocker("On-site work at Ottawa, outside NL.", gate="work_location",
             label="On-site (Ottawa)"), "On-site (Ottawa)"),
    (blocker("Personnel clearance required: Secret (profile: none).",
             label="Clearance: Secret (personnel)"), "Clearance: Secret (personnel)"),
    # Stored before rules v3 (no label): the gate's name.
    (blocker("On-site work at Ottawa, outside NL.", gate="work_location"), "Work location"),
])
def test_list_first_blocker_short_forms(client, finding, shown):
    assess(make_tender("B"), "blocked", blockers=[finding])
    assert get_list(client).context["page"].object_list[0].first_blocker == shown


def test_list_truncates_a_long_label(client):
    name = "Civil Engineering Supply Arrangement EB129-192368 for Atlantic Region"
    label = f"Supply arrangement ({name})"
    assert len(label) > 60
    assess(make_tender("B"), "blocked", blockers=[
        blocker("x", gate="supply_arrangement_required", label=label)])
    html = get_list(client).content.decode()
    assert f">{label[:59]}…</td>" in html and label not in html


def test_list_region_filter_and_title_search(client):
    make_tender("NL", region_scope="nl", title="Network cabling, St. John's")
    make_tender("NAT", region_scope="national", title="Cloud hosting services")
    make_tender("BOTH", region_scope="nl+national", title="CLOUD migration")

    assert refs(get_list(client, region="national")) == ["NAT"]
    assert refs(get_list(client, region="nl+national")) == ["BOTH"]
    assert set(refs(get_list(client, q="cloud"))) == {"NAT", "BOTH"}
    assert refs(get_list(client, q="  cloud   migration ")) == ["BOTH"]
    assert refs(get_list(client, q="cloud", region="national")) == ["NAT"]
    assert len(refs(get_list(client, region="mars"))) == 3


def test_list_closing_date_in_st_johns_time(client):
    make_tender("T", closing_date=datetime(2030, 1, 15, 17, 30, tzinfo=UTC))
    assert "2030-01-15 14:00 NST" in get_list(client).content.decode()


def test_list_paginates_25_per_page_and_keeps_filters(client):
    base = timezone.now() + timedelta(days=1)
    for i in range(30):
        make_tender(f"R-{i:02d}", region_scope="national", closing_date=base + timedelta(hours=i))
    make_tender("OTHER-REGION", region_scope="nl")

    first = get_list(client, region="national")
    assert refs(first) == [f"R-{i:02d}" for i in range(25)]
    assert first.context["page"].paginator.count == 30
    html = first.content.decode()
    assert "1–25 of 30" in html
    assert 'href="?region=national&amp;page=2"' in html

    second = get_list(client, region="national", page=2)
    assert refs(second) == [f"R-{i:02d}" for i in range(25, 30)]
    assert 'href="?region=national&amp;page=1"' in second.content.decode()

    # Out-of-range and junk page numbers fall back instead of 404ing.
    assert refs(get_list(client, region="national", page=99)) == refs(second)
    assert refs(get_list(client, region="national", page="x")) == refs(first)


def test_list_empty(client):
    assert "No notices match." in get_list(client).content.decode()


# --- detail ---------------------------------------------------------------------------

def test_detail_shows_metadata_eligibility_and_gates(client):
    data = valid_data()
    data["eligibility"]["clearance"] = {"corporate": "unknown", "personnel": "unknown",
                                        "evidence": None}
    t = make_tender("cb-1-2", publication_date=datetime(2026, 11, 2, 13, tzinfo=UTC),
                    closing_date=datetime(2030, 12, 1, 17, 30, tzinfo=UTC),
                    notice_type="Request for Proposal")
    assess(t, "blocked",
           blockers=[blocker("Bidding is limited to holders of the TBIPS supply arrangement; "
                             "the profile holds none of it.",
                             gate="supply_arrangement_required",
                             evidence="Bidders must hold a valid TBIPS supply arrangement.")],
           warnings=[blocker("On-site work at Gander, elsewhere in NL.", gate="work_location",
                             evidence="Work is performed on-site in Gander.")],
           not_stated=["supplier_origin_restriction", "clearance", "land_claims_applies"],
           data=data, flags=["ungrounded:eligibility.clearance", "truncated"])
    baseline = valid_data()
    baseline["eligibility"]["documents_behind_login"] = {
        "value": "yes", "portal": "MERX", "evidence": "MERX"}
    assess(t, "check_solicitation", model_id=KeywordExtractor.model_id, data=baseline)

    r = client.get(reverse("tender-detail", args=["cb-1-2"]))
    assert r.status_code == 200
    html = r.content.decode()
    # Metadata.
    assert "Public Services and Procurement Canada" in html
    assert "2026-11-02 09:30 NST" in html and "2030-12-01 14:00 NST" in html
    assert "Request for Proposal" in html and "NL" in html
    assert ('href="https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/cb-1-2"'
            in html)
    # Eligibility card.
    assert 'badge-blocked">Blocked<' in html
    assert "the profile holds none of it." in html
    assert "<blockquote>Bidders must hold a valid TBIPS supply arrangement.</blockquote>" in html
    assert "On-site work at Gander, elsewhere in NL." in html
    assert r.context["not_stated"] == ["Supplier origin restriction", "Security clearance",
                                       "Land claims agreement"]
    # Gate table: LLM value, evidence, per-gate flag, baseline column.
    rows = {row.gate: row for row in r.context["rows"]}
    assert len(rows) == 6
    assert rows["supply_arrangement_required"].value == "yes (TBIPS)"
    assert rows["work_location"].value == "on-site, St. John's"
    assert rows["clearance"].flags == ["quote not found in the notice: treated as unknown"]
    assert rows["documents_behind_login"].baseline == "yes (MERX)"
    assert rows["documents_behind_login"].value == "no (CanadaBuys)"
    assert "The description was too long" in html
    # The description is there, collapsed.
    assert "<details" in html and "Plain description." in html
    assert "<details open" not in html


def test_detail_unassessed_notice(client):
    make_tender("NEW")
    r = client.get(reverse("tender-detail", args=["NEW"]))
    assert r.status_code == 200
    assert "has not been assessed yet" in r.content.decode()
    assert "badge-none" in r.content.decode()


def test_detail_reference_with_colon(client):
    make_tender("SSC-25-00030887:T")
    url = reverse("tender-detail", args=["SSC-25-00030887:T"])
    assert client.get(url).status_code == 200
    assert url in get_list(client).content.decode()


def test_detail_404_for_unknown_reference(client):
    make_tender("REAL")
    r = client.get(reverse("tender-detail", args=["NOPE-123"]))
    assert r.status_code == 404
    html = r.content.decode()
    assert "Harbourline Software (fictional)" in html  # the site's own 404 page
    assert "Content-Security-Policy" in r


def test_detail_never_links_a_non_https_url(client):
    make_tender("JS", notice_url="javascript:alert(1)")
    html = client.get(reverse("tender-detail", args=["JS"])).content.decode()
    assert "javascript:" not in html
    assert "No link available" in html


# --- escaping -------------------------------------------------------------------------

XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def test_notice_text_is_escaped_everywhere(client):
    t = make_tender("XSS", title=f"Title {XSS}", description=f"Desc {XSS}",
                    buyer_org_name=f"Buyer {XSS}", notice_type=f"Type {XSS}")
    data = valid_data()
    data["eligibility"]["work_location"]["evidence"] = f"Evidence {XSS}"
    assess(t, "blocked", data=data,
           blockers=[blocker(f"Reason {XSS}", evidence=f"Quote {XSS}")],
           warnings=[blocker(f"Warn {XSS}")])

    for url in (reverse("tender-list"), reverse("tender-detail", args=["XSS"])):
        html = client.get(url).content.decode()
        assert "<script" not in html and "<img" not in html, url
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html, url

    detail = client.get(reverse("tender-detail", args=["XSS"])).content.decode()
    for prefix in ("Title", "Desc", "Buyer", "Type", "Evidence", "Reason", "Quote", "Warn"):
        assert f"{prefix} &lt;script&gt;" in detail, prefix


def test_search_box_value_is_escaped(client):
    html = get_list(client, q='"><script>alert(1)</script>').content.decode()
    assert "<script" not in html
    assert 'value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html


# --- every page -----------------------------------------------------------------------

def test_banner_and_footer(client):
    make_tender("T")
    for url in (reverse("tender-list"), reverse("tender-detail", args=["T"])):
        html = client.get(url).content.decode()
        assert ("Eligibility is assessed for a fictional firm, Harbourline Software "
                "(fictional). Always verify in the solicitation documents.") in html
        assert "Contains information licensed under the" in html
        assert 'href="https://open.canada.ca/en/open-government-licence-canada"' in html
        assert "Data updated <time" in html and "2026-11-07 23:00 NST" in html


def test_footer_with_no_data(client):
    assert "Data updated never." in get_list(client).content.decode()


# --- security -------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/?show=all", "/tenders/T/", "/tenders/missing/"])
def test_security_headers(client, path):
    make_tender("T")
    r = client.get(path)
    csp = r["Content-Security-Policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["style-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert "script-src" not in directives and "unsafe" not in csp
    assert r["X-Frame-Options"] == "DENY"
    assert r["Referrer-Policy"] == "same-origin"
    assert r["X-Content-Type-Options"] == "nosniff"


def test_pages_load_only_the_self_hosted_stylesheet(client):
    make_tender("T")
    for url in (reverse("tender-list"), reverse("tender-detail", args=["T"])):
        html = client.get(url).content.decode()
        assert '<link rel="stylesheet" href="/static/tenders/site.css">' in html
        assert "<script" not in html and "style=" not in html and "<style" not in html
        assert "http://" not in html


def test_admin_keeps_working_csp(client):
    csp = client.get("/admin/login/")["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe" not in csp


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_pages_are_get_only(client, method):
    make_tender("T")
    for url in (reverse("tender-list"), reverse("tender-detail", args=["T"])):
        assert getattr(client, method)(url).status_code == 405
