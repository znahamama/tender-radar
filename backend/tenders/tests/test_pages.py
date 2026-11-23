"""Public pages: list filters and pagination, detail, escaping, security headers."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.urls import reverse
from django.utils import timezone

from tenders.eligibility.service import assess as real_assess
from tenders.extraction.budget import CallMeter
from tenders.extraction.keyword import KeywordExtractor
from tenders.extraction.providers import AnthropicExtractor
from tenders.extraction.service import extract_tender
from tenders.ingest.schema import SOURCE_TZ
from tenders.models import Assessment, Extraction, SupplierProfile, Tender
from tenders.tests.eligibility.conftest import SA_REQUEST_NOTICE, sa_request_output
from tenders.tests.extraction.conftest import (  # noqa: F401  (autouse fixtures)
    StubClient,
    no_real_api,
    pin_llm_settings,
    response,
    tender_fields,
    valid_data,
)

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
           data=None, flags=(), created_at=None, scope="in"):
    """A stored assessment. ``scope``: in scope for an IT firm by default, so the list
    (IT notices only by default) shows it; tests of the scope toggle pass another."""
    x = Extraction.objects.create(
        tender=tender, content_hash=f"h-{model_id}-{Extraction.objects.count()}",
        model_id=model_id, prompt_version="v2", schema_version="1", status="ok",
        raw_output=data, data=data, flags=list(flags))
    if created_at:
        Extraction.objects.filter(pk=x.pk).update(created_at=created_at,
                                                  last_used_at=created_at)
    return Assessment.objects.create(
        tender=tender, extraction=x, profile=SupplierProfile.objects.order_by("id").first(),
        status=status, blockers=list(blockers), warnings=list(warnings),
        not_stated=list(not_stated), rules_version="2", inputs_hash="0" * 64,
        computed_at=timezone.now(), scope={"version": "1", "status": scope, "matches": []})


def blocker(reason, gate="clearance", evidence=None, label=None):
    f = {"gate": gate, "reason": reason, "evidence": evidence}
    if label is not None:  # findings stored before rules v3 have no label
        f["label"] = label
    return f


def refs(response):
    return [t.reference_number for t in response.context["page"].object_list]


def get_list(client, **params):
    """The list with every scope unless a test asks otherwise: most tenders here have no
    assessment, so no scope, and the default view shows IT notices only."""
    params.setdefault("scope", "all")
    params = {k: v for k, v in params.items() if v is not None}
    r = client.get(reverse("tender-list"), params)
    assert r.status_code == 200
    return r


# --- list -----------------------------------------------------------------------------

def test_baseline_model_id_matches_the_keyword_extractor():
    # pages.py spells it out instead of importing keyword.py (see BASELINE_MODEL_ID).
    from tenders.pages import BASELINE_MODEL_ID

    assert BASELINE_MODEL_ID == KeywordExtractor.model_id


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
    assert 'href="?region=national&amp;scope=all&amp;page=2"' in html

    second = get_list(client, region="national", page=2)
    assert refs(second) == [f"R-{i:02d}" for i in range(25, 30)]
    assert 'href="?region=national&amp;page=1&amp;scope=all"' in second.content.decode()

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
    t = make_tender("cb-1-2", publication_date=datetime(2026, 11, 2, tzinfo=SOURCE_TZ),
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
    assert '<dd><time datetime="2026-11-02">2026-11-02</time></dd>' in html
    assert "2030-12-01 14:00 NST" in html
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


def test_detail_shows_notes_for_sa_request_notice(client):
    """The ProServices-style request notice: the SA answer is a note, not a blocker."""
    t = Tender.objects.create(**tender_fields(**SA_REQUEST_NOTICE))
    extractor = AnthropicExtractor(StubClient(response(sa_request_output())), retry_wait_s=0)
    meter = CallMeter(max_calls=5, daily_budget_usd=Decimal("0.50"))
    real_assess(extract_tender(t, extractor, meter).extraction)

    r = client.get(reverse("tender-detail", args=[t.reference_number]))
    html = r.content.decode()
    assert 'badge-check">Check solicitation<' in html
    assert "<h3>Blockers</h3>" not in html
    assert html.index("<h3>Warnings</h3>") < html.index("<h3>Notes</h3>")
    assert ('<span class="gate">Supply arrangement required</span>: This notice is how firms '
            'qualify for the ProServices arrangement.') in html
    assert "<blockquote>Request for Supply Arrangement</blockquote>" in html


@pytest.mark.parametrize(("stored_utc", "shown"), [
    (datetime(2026, 11, 2, 5, tzinfo=UTC), date(2026, 11, 2)),   # midnight EST
    (datetime(2026, 7, 1, 4, tzinfo=UTC), date(2026, 7, 1)),     # midnight EDT
])
def test_detail_published_is_the_source_date(client, stored_utc, shown):
    make_tender("P", publication_date=stored_utc)
    r = client.get(reverse("tender-detail", args=["P"]))
    assert r.context["published"] == shown
    published = r.content.decode().split("<dt>Published</dt>")[1].split("</dd>")[0]
    assert published == f'<dd><time datetime="{shown}">{shown}</time>'


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


def test_detail_shows_no_link_when_the_csv_has_none(client):
    make_tender("PW-__HN-446-81465", notice_url="")
    html = client.get(reverse("tender-detail", args=["PW-__HN-446-81465"])).content.decode()
    assert "No link available" in html
    assert "/tender-notice/" not in html and "canadabuys.canada.ca" not in html


def test_detail_never_links_a_guessed_url(client):
    """Rows stored before migration 0012 may still hold a URL the old ingest built."""
    ref = "PW-_NCS-030-11940"
    make_tender(ref, notice_url_derived=True)  # make_tender's default URL is the guessed shape
    html = client.get(reverse("tender-detail", args=[ref])).content.decode()
    assert "No link available" in html
    assert "/tender-notice/" not in html


def test_detail_links_the_csv_url_even_off_canadabuys(client):
    url = "https://discovery.ariba.com/rfx/19419533"
    make_tender("WS1", notice_url=url)
    html = client.get(reverse("tender-detail", args=["WS1"])).content.decode()
    assert f'href="{url}"' in html


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


@pytest.mark.parametrize("path", ["/tenders/T/", "/admin/login/"])
def test_every_security_header_over_https(client, path):
    """One public page and the admin, as API Gateway delivers them (HTTPS)."""
    make_tender("T")
    r = client.get(path, secure=True)
    assert r.status_code == 200
    assert r["Strict-Transport-Security"] == "max-age=31536000"  # no subdomains, no preload
    csp = r["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe" not in csp
    assert r["X-Content-Type-Options"] == "nosniff"
    assert r["X-Frame-Options"] == "DENY"
    assert r["Referrer-Policy"] == "same-origin"
    pp = dict(d.strip().split("=") for d in r["Permissions-Policy"].split(","))
    assert pp["camera"] == pp["microphone"] == pp["geolocation"] == "()"
    assert set(pp.values()) == {"()"}
    if path.startswith("/admin/"):
        csrf = r.cookies["csrftoken"]
        assert csrf["httponly"] is True and csrf["samesite"] == "Lax"


def test_no_hsts_over_plain_http(client):
    """Local runserver is http; HSTS there would pin localhost to HTTPS in the browser."""
    assert "Strict-Transport-Security" not in client.get("/")


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_pages_are_get_only(client, method):
    make_tender("T")
    for url in (reverse("tender-list"), reverse("tender-detail", args=["T"])):
        assert getattr(client, method)(url).status_code == 405


CAUTION = ("Parts of this notice look like instructions to an AI system; treat its extracted "
           "fields with extra care.")


@pytest.mark.parametrize(("data_quality", "flags", "shown"), [
    ([], [], False),
    (["injection_pattern"], [], True),            # the code-side detector, at ingest
    ([], ["injection_suspected"], True),          # the model's own flag
    (["mentions_standing_offer"], ["truncated"], False),
])
def test_detail_injection_caution(client, data_quality, flags, shown):
    t = make_tender("INJ", data_quality=data_quality)
    assess(t, "check_solicitation", data=valid_data(), flags=flags)
    html = client.get(reverse("tender-detail", args=["INJ"])).content.decode()
    assert (CAUTION in html) is shown
    assert ('class="caution" role="note"' in html) is shown


def test_detail_injection_caution_without_any_extraction(client):
    make_tender("INJ2", data_quality=["injection_pattern"])
    assert CAUTION in client.get(reverse("tender-detail", args=["INJ2"])).content.decode()


def test_not_stated_labels_name_the_clearance_side():
    from tenders.pages import not_stated_labels

    gates = {"clearance": {"corporate": "none", "personnel": "unknown", "evidence": "x"}}
    assert not_stated_labels(["supply_arrangement_required", "clearance"], gates) == [
        "Supply arrangement required", "Security clearance (personnel)"]
    gates["clearance"] = {"corporate": "unknown", "personnel": "secret", "evidence": "x"}
    assert not_stated_labels(["clearance"], gates) == ["Security clearance (corporate)"]
    gates["clearance"] = {"corporate": "unknown", "personnel": "unknown", "evidence": None}
    assert not_stated_labels(["clearance"], gates) == ["Security clearance"]
    assert not_stated_labels(["clearance"], {}) == ["Security clearance"]  # failed extraction


def test_daily_baseline_step_fills_the_baseline_column_without_the_api(client):
    """The worker's real baseline step: no LLM call (the autouse no_real_api fixture fails
    any client construction), and the detail page then shows the baseline column."""
    from django.core.management import call_command

    import tenders.worker
    from tenders.models import LlmCall

    step = tenders.worker.TASKS["daily"][1]
    assert step == ("extract_tenders", ["--status", "Open", "--baseline"])
    t = make_tender("cb-base-1", description="Security Requirement: None. Work is remote.")
    make_tender("cb-base-2", tender_status="Expired")  # not Open: not extracted
    call_command(step[0], *step[1], stdout=StringIO())
    assert LlmCall.objects.count() == 0
    x = Extraction.objects.get(tender=t)
    assert (x.model_id, x.status) == (KeywordExtractor().model_id, "ok")
    assert not Extraction.objects.filter(tender__reference_number="cb-base-2").exists()
    r = client.get(reverse("tender-detail", args=["cb-base-1"]))
    assert "No keyword baseline run for this notice." not in r.content.decode()
    assert r.context["baseline"] == x


# --- solicitation documents (prompt v4) and the search link -------------------------------

DOC_URL = "https://canadabuys.canada.ca/documents/pub/att/2026/11/01/abc123/PW-1.EBSU000.PDF"
# Invented: never shown on the page except the quoted clause.
DOC_TEXT_UNQUOTED = "Section 9 describes the invented pricing schedule in full detail."


def assess_v4(tender, documents, *, quote_source="PW-1.EBSU000.PDF", injection=False):
    data = valid_data()
    for g in data["eligibility"].values():
        g["source"] = "notice" if g.get("evidence") else None
    data["eligibility"]["clearance"] = {
        "corporate": "secret", "personnel": "secret", "source": quote_source,
        "evidence": "must hold a valid Facility Security Clearance at the level of SECRET"}
    a = assess(tender, "check_solicitation", data=data,
               blockers=[blocker("Needs a Secret FSC; the profile holds Reliability.",
                                 evidence=data["eligibility"]["clearance"]["evidence"])])
    Extraction.objects.filter(pk=a.extraction_id).update(
        prompt_version="v4", schema_version="2",
        documents=[{"name": n, "url": u, "status": s, "chars": 10, "total_chars": 10,
                    "injection": injection} for n, u, s in documents])
    return a


def test_detail_names_the_document_a_gate_was_answered_from(client):
    t = make_tender("PW-1")
    assess_v4(t, [("PW-1.EBSU000.PDF", DOC_URL, "full"),
                  ("annex.pdf", DOC_URL.replace("PW-1.EBSU000.PDF", "annex.pdf"), "full"),
                  ("left-out.pdf", DOC_URL.replace("PW-1.EBSU000.PDF", "x.pdf"), "not_read")])
    r = client.get(reverse("tender-detail", args=["PW-1"]))
    html = r.content.decode()
    rows = {row.gate: row for row in r.context["rows"]}
    assert rows["clearance"].document.name == "PW-1.EBSU000.PDF"
    assert rows["clearance"].document.url == DOC_URL
    # Notice-sourced gates name no document.
    assert rows["work_location"].document is None and rows["work_location"].evidence
    line = (f'From the solicitation documents: <a href="{DOC_URL}" rel="external noreferrer">'
            "PW-1.EBSU000.PDF</a>")
    assert html.count(line) == 2  # the gate row and the blocker that quotes it
    # Footer: the documents read (not the one left out) and the copyright line.
    assert r.context["documents_read"][1].name == "annex.pdf"
    assert len(r.context["documents_read"]) == 2 and "left-out.pdf" not in html
    assert "the model read 2 solicitation documents published on CanadaBuys" in html
    assert "Copyright in these documents stays with the Government of Canada" in html
    assert "Not stated in the notice or its documents" in html


def test_detail_never_shows_document_text_beyond_quotes(client, monkeypatch):
    """The page reads Extraction.documents (names and links), never TenderDocument.text."""
    from tenders.models import TenderDocument

    t = make_tender("PW-1")
    TenderDocument.objects.create(tender=t, url=DOC_URL, status="ok", text=DOC_TEXT_UNQUOTED,
                                  redact_version=3, fetched_at=timezone.now())
    assess_v4(t, [("PW-1.EBSU000.PDF", DOC_URL, "full")])
    html = client.get(reverse("tender-detail", args=["PW-1"])).content.decode()
    assert "invented pricing schedule" not in html
    assert "at the level of SECRET" in html  # the quote itself is shown


def test_detail_document_link_only_for_canadabuys_document_urls(client):
    t = make_tender("PW-1")
    assess_v4(t, [("PW-1.EBSU000.PDF", "javascript:alert(1)", "full")])
    html = client.get(reverse("tender-detail", args=["PW-1"])).content.decode()
    assert "javascript:" not in html
    assert "From the solicitation documents: PW-1.EBSU000.PDF" in html


def test_detail_unknown_source_name_shows_no_document_line(client):
    t = make_tender("PW-1")
    assess_v4(t, [("PW-1.EBSU000.PDF", DOC_URL, "full")], quote_source="other.pdf")
    html = client.get(reverse("tender-detail", args=["PW-1"])).content.decode()
    assert "From the solicitation documents" not in html


def test_detail_without_documents_has_no_document_lines(client):
    t = make_tender("PW-1")
    assess(t, "eligible", data=valid_data())
    html = client.get(reverse("tender-detail", args=["PW-1"])).content.decode()
    assert "From the solicitation documents" not in html and "Copyright" not in html
    assert "Not stated in the notice</h3>" in html


def test_detail_cautions_when_a_document_read_was_flagged(client):
    t = make_tender("PW-1")
    assess_v4(t, [("PW-1.EBSU000.PDF", DOC_URL, "full")], injection=True)
    r = client.get(reverse("tender-detail", args=["PW-1"]))
    assert r.context["injection_caution"]
    t2 = make_tender("PW-2")
    assess_v4(t2, [("PW-1.EBSU000.PDF", DOC_URL, "not_read")], injection=True)
    assert not client.get(reverse("tender-detail", args=["PW-2"])).context["injection_caution"]


def test_detail_v4_flag_wording():
    from tenders.pages import gate_flags

    flags = ["ungrounded:eligibility.clearance", "ungrounded_detail:eligibility.work_location.place"]
    assert gate_flags("clearance", flags) == ["quote not found in the notice: treated as unknown"]
    assert gate_flags("clearance", flags, True) == [
        "quote not found in its source: treated as unknown"]
    assert gate_flags("work_location", flags, True) == [
        "place not found in the notice or its documents: dropped"]


def test_detail_search_link_when_the_csv_has_no_url(client):
    make_tender("PW-1", notice_url="", solicitation_number="EW479-162880/C")
    r = client.get(reverse("tender-detail", args=["PW-1"]))
    html = r.content.decode()
    url = "https://canadabuys.canada.ca/en/tender-opportunities?words=EW479-162880%2FC"
    assert r.context["search_url"] == url
    assert (f'<a href="{url}" rel="external noreferrer">Search CanadaBuys for solicitation '
            "EW479-162880/C</a>") in html
    assert "a search on CanadaBuys, not a link to this notice" in html
    assert "No link available" in html  # the official-notice field stays honest


@pytest.mark.parametrize(("fields", "shown"), [
    ({"solicitation_number": "EW479-162880/C"}, False),  # the CSV gave a notice URL
    ({"notice_url": "", "solicitation_number": ""}, False),  # nothing to search for
    ({"notice_url": "", "solicitation_number": "  "}, False),
    ({"notice_url": "", "reference_number": "cb-461-59595377"}, False),  # never the reference
])
def test_detail_no_search_link(client, fields, shown):
    ref = fields.pop("reference_number", "PW-9")
    make_tender(ref, **fields)
    r = client.get(reverse("tender-detail", args=[ref]))
    assert (r.context["search_url"] is not None) is shown
    assert "Search CanadaBuys" not in r.content.decode()


def test_search_link_escapes_odd_characters(client):
    make_tender("PW-1", notice_url="", solicitation_number='A&B "x" <y>')
    r = client.get(reverse("tender-detail", args=["PW-1"]))
    assert r.context["search_url"].endswith("?words=A%26B+%22x%22+%3Cy%3E")
    assert "<y>" not in r.content.decode()


# --- scope (IT work or not) ------------------------------------------------------------

def _scoped(client):
    for ref, scope in (("IT-1", "in"), ("IT-2", "in"), ("NOT-IT", "out"),
                       ("NO-CODES", "unknown")):
        assess(make_tender(ref), "check_solicitation", scope=scope)
    make_tender("UNASSESSED")


def test_list_shows_it_notices_by_default_with_both_counts(client):
    _scoped(client)
    r = get_list(client, scope=None)
    assert sorted(refs(r)) == ["IT-1", "IT-2"]
    html = r.content.decode()
    assert 'Showing 2 IT notices · <a href="?scope=all">Show all 5</a>' in html
    assert html.count('class="badge badge-scope-in"') == 2


def test_list_toggle_shows_every_notice_with_its_scope_badge(client):
    _scoped(client)
    r = get_list(client, scope="all")
    assert sorted(refs(r)) == ["IT-1", "IT-2", "NO-CODES", "NOT-IT", "UNASSESSED"]
    html = r.content.decode()
    assert 'Showing all 5 notices · <a href="?">Show 2 IT notices only</a>' in html
    assert ">Not IT</span>" in html and ">No codes</span>" in html
    assert '<input type="hidden" name="scope" value="all">' in html  # the form keeps it


def test_scope_counts_follow_the_other_filters(client):
    _scoped(client)
    assess(make_tender("NAT-IT", region_scope="national"), "blocked", scope="in")
    html = get_list(client, scope=None, region="national").content.decode()
    assert "Showing 1 IT notices · " in html and "Show all 1</a>" in html


def test_scope_never_changes_the_eligibility_badge(client):
    assess(make_tender("OUT-BLOCKED"), "blocked", scope="out",
           blockers=[blocker("Clearance required.")])
    html = client.get(reverse("tender-detail", args=["OUT-BLOCKED"])).content.decode()
    assert '<span class="badge badge-blocked">Blocked</span>' in html
    assert "Scope:</strong>\n  outside IT work for this firm" in html


def _detail(client, ref):
    return client.get(reverse("tender-detail", args=[ref])).content.decode()


def test_detail_scope_line_names_the_matched_codes_with_titles(client):
    from tenders.models import CategoryCode

    CategoryCode.objects.create(scheme="unspsc", code="43230000", description="Software")
    t = make_tender("IN", unspsc_codes=["43230000", "72151100"], gsin_codes=["n7030"])
    a = assess(t, "check_solicitation")
    a.scope = {"version": "1", "status": "in", "matches": [
        {"scheme": "unspsc", "code": "43230000",
         "family": "IT and telecom equipment and software"},
        {"scheme": "gsin", "code": "N7030", "family": "ADP equipment and software"}]}
    a.save()
    html = _detail(client, "IN")
    assert ("Scope:</strong>\n  IT work (matched UNSPSC 43230000 Software; "
            "GSIN N7030 ADP equipment and software)") in html  # no title stored: the family
    assert "72151100" not in html.split("Scope:")[1].split("</p>")[0]
    assert html.index("Scope:") < html.index('id="elig-heading"')


def test_detail_scope_line_lists_the_codes_when_outside_it(client):
    from tenders.models import CategoryCode

    CategoryCode.objects.create(scheme="unspsc", code="72151100",
                                description="Plumbing construction services")
    codes = ["72151100", *(f"{30000000 + i}" for i in range(11))]
    assess(make_tender("OUT", unspsc_codes=codes), "check_solicitation", scope="out")
    line = _detail(client, "OUT").split("Scope:")[1].split("</p>")[0]
    assert "outside IT work for this firm (codes: UNSPSC 72151100 Plumbing construction services;" in line
    assert line.count("UNSPSC ") == 10 and "and 2 more)" in line


def test_detail_scope_line_unknown_and_not_assessed(client):
    assess(make_tender("NONE"), "check_solicitation", scope="unknown")
    assert "unknown: the notice lists no UNSPSC or GSIN category code" in _detail(client, "NONE")
    make_tender("NEW")
    assert "Scope:" not in _detail(client, "NEW")
