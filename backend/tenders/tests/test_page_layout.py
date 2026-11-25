"""The public pages' layout: the list as one panel, the detail page verdict first.

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

def test_list_is_one_panel_heading_toolbar_table_pager(client):
    _board(client)
    html = get_list(client, scope=None).content.decode()
    order = ['<section class="panel"', '<h1 id="list-heading">Open notices</h1>',
             f'<p class="lede">{INTRO}</p>', '<form class="toolbar"', '<table class="tenders',
             'class="panel-foot"', '</section>']
    assert [html.index(s) for s in order] == sorted(html.index(s) for s in order)
    assert html.count("<form") == 1  # one GET form replaces the counts, toggles and filters
    assert "<h1 id=\"list-heading\">All notices</h1>" in get_list(client, show="all").content.decode()


def test_list_status_control_counts_each_status(client):
    _board(client)
    html = get_list(client, scope=None).content.decode()
    assert '<option value="">All 3</option>' in html
    for value, label in (("blocked", "Blocked"), ("check_solicitation", "Check solicitation"),
                         ("no_blockers_stated", "No blockers stated")):
        assert f'<option value="{value}">{label} 1</option>' in html
    assert 'value="not_assessed"' not in html  # listed only when there is one


def test_list_toolbar_marks_the_picked_values_and_its_links_keep_other_filters(client):
    _board(client)
    html = get_list(client, status="blocked", region="nl").content.decode()
    assert '<option value="blocked" selected>Blocked 1</option>' in html
    assert '<option value="nl" selected>' in html
    # {% querystring %} keeps the request's own parameter order.
    assert ('<a href="?status=blocked&amp;region=nl&amp;scope=all" aria-current="true">Open</a>'
            '<a href="?status=blocked&amp;region=nl&amp;scope=all&amp;show=all">All</a>') in html
    assert ('<a href="?status=blocked&amp;region=nl">IT <span class="seg-n">1</span></a>'
            '<a href="?status=blocked&amp;region=nl&amp;scope=all" aria-current="true">'
            'All notices <span class="seg-n">1</span></a>') in html
    assert html.count('aria-current="true"') == 2  # one per toggle


def test_list_rows_are_reference_notice_status_closes_reason(client):
    _board(client)
    html = get_list(client).content.decode()
    row = re.search(r'<tr class="row row-blocked">(.*?)</tr>', html, re.DOTALL).group(1)
    cells = re.findall(r'<td class="cell-(\w+)"', row)
    assert cells == ["ref", "notice", "status", "closes", "reason"]
    assert '<td class="cell-ref">B</td>' in row
    # Scope chip before the title; buyer, region and type in a tooltip and for screen readers.
    assert row.index('class="badge badge-scope-in"') < row.index('class="notice-title"')
    meta = "Public Services and Procurement Canada · NL · Regular"
    assert f'title="{meta}">Tender B</a>' in row
    assert f'<span class="notice-meta">{meta}</span>' in row
    assert '<span class="badge badge-blocked">Blocked</span>' in row  # a word, not a colour
    assert ">Supply arrangement (SBIPS)</td>" in row
    assert '<span class="until">in 1 week</span>' in row  # open view: time left, short
    check = re.search(r'<tr class="row row-check_solicitation">(.*?)</tr>', html, re.DOTALL).group(1)
    assert ">2 gates not stated</td>" in check
    clear = re.search(r'<tr class="row row-no_blockers_stated">(.*?)</tr>', html, re.DOTALL).group(1)
    assert '<span class="none">—</span></td>' in clear


def test_list_unassessed_rows_and_closed_view(client):
    make_tender("U", closing_date=None)
    html = get_list(client).content.decode()
    assert '<tr class="row row-not_assessed">' in html and ">Not stated</td>" in html
    assert 'class="until"' not in get_list(client, show="all").content.decode()


def test_list_toolbar_is_one_get_form_with_an_apply_button(client):
    _board(client)
    html = get_list(client, show="all").content.decode()
    form = re.search(r'<form class="toolbar" method="get"(.*?)</form>', html, re.DOTALL).group(1)
    for part in ('placeholder="Filter notices…"', 'name="status"', 'name="region"',
                 '<input type="hidden" name="show" value="all">',
                 '<input type="hidden" name="scope" value="all">',
                 '<button class="apply" type="submit">Apply</button>'):
        assert part in form
    assert 'class="reset"' not in form  # nothing to reset
    filtered = get_list(client, q="Tender").content.decode()
    assert '<a class="reset" href="?scope=all">Reset</a>' in filtered


def test_list_pager_links_and_greys_out_its_ends(client):
    for i in range(30):
        make_tender(f"R-{i:02d}")
    first = get_list(client).content.decode()
    assert "1–25 of 30" in first and "Page 1 of 2" in first
    assert first.count('<span class="pager-btn" aria-hidden="true">') == 2  # « ‹
    assert '<a class="pager-btn" href="?scope=all&amp;page=2" aria-label="Last page">»</a>' in first
    second = get_list(client, page=2).content.decode()  # the URL is ?page=2&scope=all
    assert '<a class="pager-btn" href="?page=1&amp;scope=all" aria-label="First page">«</a>' in second
    assert second.count('<span class="pager-btn" aria-hidden="true">') == 2  # › »


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
