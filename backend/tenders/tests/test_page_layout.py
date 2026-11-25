"""The public pages' layout: the list as a triage board, the detail page verdict first.

Wording and data are tested in test_pages.py; this pins the order and structure the demo
relies on, and that status is never shown by colour alone."""

import itertools
import re

import pytest
from django.urls import reverse

from tenders.tests.extraction.conftest import (  # noqa: F401  (autouse fixtures)
    no_real_api,
    pin_llm_settings,
    valid_data,
)
from tenders.tests.test_pages import assess, blocker, get_list, make_tender, pin_model  # noqa: F401

pytestmark = pytest.mark.django_db

INTRO = ("Federal IT tenders from CanadaBuys, checked against six eligibility gates for one "
         "fictional St. John's IT firm. Every answer comes with the exact quote behind it.")


def _board(client):
    assess(make_tender("B"), "blocked", blockers=[
        blocker("x", gate="supply_arrangement_required", label="Supply arrangement (SBIPS)")])
    assess(make_tender("C"), "check_solicitation", not_stated=["clearance", "work_location"])
    assess(make_tender("N"), "no_blockers_stated")


# --- list -------------------------------------------------------------------------------

def test_list_leads_with_the_intro_and_the_status_summary(client):
    _board(client)
    html = get_list(client, scope=None).content.decode()
    assert f'<p class="lede">{INTRO}</p>' in html
    assert html.index("lede") < html.index('class="triage"') < html.index('class="tenders')
    assert '<span class="triage-n">3</span> <span class="triage-label">IT notices</span>' in html
    for value, label in (("blocked", "Blocked"), ("check_solicitation", "Check solicitation"),
                         ("no_blockers_stated", "No blockers stated")):
        assert (f'<a class="triage-{value}" href="?status={value}"><span class="triage-n">1'
                f'</span> <span class="triage-label">{label}</span></a>') in html
    assert "triage-not_assessed" not in html  # listed only when there is one


def test_list_summary_marks_the_picked_status_and_keeps_other_filters(client):
    _board(client)
    html = get_list(client, status="blocked", region="nl").content.decode()
    # {% querystring %} keeps the request's own parameter order.
    assert ('<a class="triage-blocked" href="?status=blocked&amp;region=nl&amp;scope=all"'
            ' aria-current="true">') in html
    assert '<a class="triage-all" href="?region=nl&amp;scope=all">' in html
    assert html.count('aria-current="true"') == 1


def test_list_rows_lead_with_status_and_end_with_the_reason(client):
    _board(client)
    html = get_list(client).content.decode()
    row = re.search(r'<tr class="row row-blocked">(.*?)</tr>', html, re.DOTALL).group(1)
    cells = re.findall(r'<td class="cell-(\w+)"', row)
    assert cells == ["status", "notice", "closes", "reason"]
    assert '<span class="badge badge-blocked">Blocked</span>' in row  # a word, not a colour
    assert ">Supply arrangement (SBIPS)</td>" in row
    check = re.search(r'<tr class="row row-check_solicitation">(.*?)</tr>', html, re.DOTALL).group(1)
    assert ">2 gates not stated</td>" in check
    assert '<span class="until">in ' in row  # open view: time left


def test_list_unassessed_rows_and_closed_view(client):
    make_tender("U", closing_date=None)
    html = get_list(client).content.decode()
    assert '<tr class="row row-not_assessed">' in html and ">Not stated</td>" in html
    assert 'class="until"' not in get_list(client, show="all").content.decode()


def test_list_filters_fold_unless_a_filter_is_on(client):
    _board(client)
    assert '<details class="filters-panel">' in get_list(client).content.decode()
    assert '<details class="filters-panel" open>' in get_list(client, q="Tender").content.decode()


def test_list_it_only_view_marks_its_table(client):
    _board(client)
    assert '<table class="tenders only-it">' in get_list(client, scope=None).content.decode()
    assert '<table class="tenders">' in get_list(client).content.decode()


# --- detail -----------------------------------------------------------------------------

def _detail(client, ref):
    return client.get(reverse("tender-detail", args=[ref])).content.decode()


def test_detail_reads_verdict_then_gates_then_technical_then_about(client):
    t = make_tender("D", solicitation_number="X-1", notice_url="")
    assess(t, "blocked", data=valid_data(), flags=["truncated"],
           blockers=[blocker("Personnel clearance required: Secret (profile: none).",
                             evidence="The work requires a secret clearance.")])
    html = _detail(client, "D")
    order = ['<h1>', '<dt>Closes</dt>', 'class="card verdict verdict-blocked"',
             '<h3>Blockers</h3>', 'id="gates-heading"', '<details class="technical">',
             'Assessed ', 'Keyword baseline', 'id="about-heading"', '<dt>Published</dt>',
             'Search CanadaBuys', '<details class="description">']
    positions = [html.index(s) for s in order]
    assert positions == sorted(positions), order
    # The main gates table has three columns; From, Flags and the baseline are folded.
    main = html.split('id="gates-heading"')[1].split('<details class="technical">')[0]
    assert re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', main) == [
        "Gate", "LLM value", "Evidence"]
    technical = html.split('<details class="technical">')[1].split("</details>")[0]
    assert "Keyword baseline" in technical and "The description was too long" in technical


def test_detail_headings_are_in_order(client):
    t = make_tender("H")
    assess(t, "check_solicitation", data=valid_data(), flags=["truncated"],
           warnings=[blocker("A comprehensive land claims agreement applies.",
                             gate="land_claims_applies")])
    levels = [int(n) for n in re.findall(r"<h([1-6])[ >]", _detail(client, "H"))]
    assert levels[0] == 1 and levels.count(1) == 1
    assert all(b - a <= 1 for a, b in itertools.pairwise(levels)), levels


def test_base_declares_both_colour_schemes(client):
    make_tender("T")
    for url in (reverse("tender-list"), reverse("tender-detail", args=["T"])):
        assert '<meta name="color-scheme" content="light dark">' in client.get(url).content.decode()


def test_unassessed_detail_has_a_neutral_verdict_card(client):
    make_tender("NEW")
    html = _detail(client, "NEW")
    assert 'class="card verdict verdict-not_assessed"' in html
    assert '<span class="badge badge-none">Not assessed</span>' in html
