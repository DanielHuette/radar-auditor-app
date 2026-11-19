from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from radarauditor.collectors.snapshot import (
    BusinessFacts,
    ImageInfo,
    LinkInfo,
    LinkKind,
    RawAudit,
    SitemapEntry,
    UrlProbe,
)
from radarauditor.db.models import CheckOutcome
from radarauditor.errors import ScoringError
from radarauditor.scoring.catalog import find_check, load_catalogue
from radarauditor.scoring.checks.b_struktur import run
from radarauditor.scoring.checks.crawl import is_descriptive_filename

SITE = "https://www.dachdeckerei-muster.de"
START = f"{SITE}/"
SUBPAGE = f"{SITE}/ueber-uns/"
SERVICE_PAGE = f"{SITE}/dachsanierung/"
CONTENT_PAGES = 12
SAME_PAGE_LIMITS = find_check(
    load_catalogue(Path(__file__).resolve().parent.parent / "config" / "pruefkatalog.yaml"), "b22"
).thresholds
THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "b3": {},
    "b4": {},
    "b5": {},
    "b10": {},
    "b11": {},
    "b12": {},
    "b13": {},
    "b14": {},
    "b15": {},
    "b16": {},
    "b17": {},
    "b18": {},
    "b19": {},
    "b20": {},
    "b21": {},
    "b22": SAME_PAGE_LIMITS,
}
Edit = Callable[..., RawAudit]
Outcome = CheckOutcome
SHARE_BOUNDARY = [
    (0, Outcome.PASSED),
    (1, Outcome.PARTIAL),
    (CONTENT_PAGES // 2, Outcome.PARTIAL),
    (CONTENT_PAGES // 2 + 1, Outcome.PARTIAL),
    (CONTENT_PAGES - 1, Outcome.PARTIAL),
    (CONTENT_PAGES, Outcome.FAILED),
]


def _run(check_id: str, raw: RawAudit) -> tuple[CheckOutcome, dict[str, Any]]:
    outcome, measured = run(check_id, raw, THRESHOLDS[check_id])
    return outcome, dict(measured)


def _map_pages(raw: RawAudit, with_website: Edit, change: Callable[[int, Any], Any]) -> RawAudit:
    assert raw.website is not None
    pages = [change(index, page) for index, page in enumerate(raw.website.pages)]
    return with_website(raw, pages=pages)


def _update_first(raw: RawAudit, with_website: Edit, count: int, **updates: Any) -> RawAudit:
    return _map_pages(
        raw,
        with_website,
        lambda index, page: page.model_copy(update=updates) if index < count else page,
    )


def _update_last(raw: RawAudit, with_website: Edit, count: int, **updates: Any) -> RawAudit:
    first_changed = CONTENT_PAGES - count
    return _map_pages(
        raw,
        with_website,
        lambda index, page: (
            page.model_copy(update=updates) if first_changed <= index < CONTENT_PAGES else page
        ),
    )


def _update_all(raw: RawAudit, with_website: Edit, **updates: Any) -> RawAudit:
    return _update_first(raw, with_website, CONTENT_PAGES + 1, **updates)


def _update_url(raw: RawAudit, with_website: Edit, url: str, **updates: Any) -> RawAudit:
    return _map_pages(
        raw,
        with_website,
        lambda _, page: page.model_copy(update=updates) if str(page.url) == url else page,
    )


@pytest.mark.parametrize("check_id", sorted(THRESHOLDS))
def test_full_audit_passes_or_names_why_it_cannot_be_checked(
    raw_audit_full: RawAudit, check_id: str
) -> None:
    outcome, measured = _run(check_id, raw_audit_full)
    if check_id == "b12":
        assert (outcome, measured) == (
            Outcome.NOT_MEASURABLE,
            {"reason": "not_verifiable_from_outside"},
        )
    else:
        assert outcome is Outcome.PASSED, measured


@pytest.mark.parametrize("check_id", sorted(THRESHOLDS))
def test_minimal_audit_is_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    outcome, measured = _run(check_id, raw_audit_minimal)
    assert outcome is Outcome.NOT_MEASURABLE
    expected = (
        "not_verifiable_from_outside"
        if check_id == "b12"
        else "no_business_name"
        if check_id == "b21"
        else "no_website"
    )
    assert measured == {"reason": expected}


@pytest.mark.parametrize("check_id", ["b10", "b13", "b15", "b16", "b17", "b18", "b20", "b22"])
def test_checks_on_new_crawl_data_say_so_for_a_stored_old_crawl(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    assert raw_audit_full.website is not None
    old = _update_all(
        raw_audit_full,
        with_website,
        body_text=None,
        first_text=None,
        images=None,
        links=None,
        last_modified=None,
    )
    old = with_website(old, agent_fetches=None, variant_probes=None)
    if check_id == "b22":
        assert _run(check_id, old)[1] == {"reason": "no_agent_fetches"}
        return
    assert _run(check_id, old) == (Outcome.NOT_MEASURABLE, {"reason": "crawl_data_outdated"})


@pytest.mark.parametrize(("broken", "expected"), SHARE_BOUNDARY)
def test_b3_share_of_pages_without_a_description(
    raw_audit_full: RawAudit, with_website: Edit, broken: int, expected: CheckOutcome
) -> None:
    raw = _update_last(raw_audit_full, with_website, broken, meta_description=None)
    outcome, measured = _run("b3", raw)
    assert outcome is expected
    assert len(measured["problems_by_page"]) == min(broken, 10)


def test_b3_a_missing_title_and_a_repeated_title_are_named(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    assert raw_audit_full.website is not None
    first, second, third = raw_audit_full.website.pages[:3]
    raw = _update_url(raw_audit_full, with_website, str(first.url), title="  ")
    raw = _update_url(raw, with_website, str(third.url), title=second.title)
    outcome, measured = _run("b3", raw)
    assert outcome is Outcome.FAILED
    assert measured["problems_by_page"][str(first.url)] == ["no_title"]
    assert measured["problems_by_page"][str(second.url)] == ["duplicate_title"]
    assert measured["problems_by_page"][str(third.url)] == ["duplicate_title"]
    assert len(measured["duplicate_titles"]) == 1


def test_b3_a_repeated_description_is_named(raw_audit_full: RawAudit, with_website: Edit) -> None:
    assert raw_audit_full.website is not None
    first, second = raw_audit_full.website.pages[:2]
    raw = _update_url(
        raw_audit_full, with_website, str(second.url), meta_description=first.meta_description
    )
    outcome, measured = _run("b3", raw)
    assert outcome is Outcome.FAILED
    assert measured["problems_by_page"][str(first.url)] == ["duplicate_description"]


def test_b3_problems_on_subpages_only_are_partial_and_list_each_page(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(raw_audit_full, with_website, SUBPAGE, meta_description=None)
    outcome, measured = _run("b3", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["problems_by_page"] == {SUBPAGE: ["no_description"]}


@pytest.mark.parametrize(("broken", "expected"), SHARE_BOUNDARY)
def test_b4_share_of_pages_without_a_main_heading(
    raw_audit_full: RawAudit, with_website: Edit, broken: int, expected: CheckOutcome
) -> None:
    raw = _update_last(raw_audit_full, with_website, broken, headings=[(2, "Nur Unterüberschrift")])
    outcome, measured = _run("b4", raw)
    assert outcome is expected
    assert set(sum(measured["problems_by_page"].values(), [])) <= {"no_h1"}


def test_b4_a_skipped_heading_level_is_named(raw_audit_full: RawAudit, with_website: Edit) -> None:
    raw = _update_url(raw_audit_full, with_website, SUBPAGE, headings=[(1, "Titel"), (3, "Sprung")])
    outcome, measured = _run("b4", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["problems_by_page"][SUBPAGE] == ["h1_to_h3"]


def _navigation(kinds: list[LinkKind]) -> list[LinkInfo]:
    return [
        LinkInfo(
            kind=kind,
            text=f"Punkt {index}",
            url=f"{SITE}/p{index}/" if kind == "href" else None,
            in_navigation=True,
        )
        for index, kind in enumerate(kinds)
    ]


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (["href", "href"], Outcome.PASSED),
        (["href", "javascript"], Outcome.FAILED),
        (["onclick", "javascript"], Outcome.FAILED),
        (["no_href", "href", "href", "href"], Outcome.FAILED),
        (["no_href", "href", "onclick", "href", "javascript"], Outcome.FAILED),
    ],
)
def test_b5_navigation_items_without_a_real_link(
    raw_audit_full: RawAudit, with_website: Edit, kinds: list[LinkKind], expected: CheckOutcome
) -> None:
    raw = _update_all(raw_audit_full, with_website, links=_navigation(kinds))
    outcome, measured = _run("b5", raw)
    assert outcome is expected
    assert measured["navigation_measured"] is True
    assert measured["navigation_items"] == len(kinds)
    assert measured["navigation_without_href_count"] == sum(kind != "href" for kind in kinds)


def test_b5_a_navigation_item_without_a_link_on_a_subpage_only_is_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full, with_website, SUBPAGE, links=_navigation(["href", "javascript"])
    )
    outcome, measured = _run("b5", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["navigation_without_href_count"] == 1


def test_b5_without_navigation_data_only_the_link_graph_counts(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, links=None)
    outcome, measured = _run("b5", raw)
    assert outcome is Outcome.PASSED
    assert measured["navigation_measured"] is False


def test_b5_links_outside_the_navigation_are_not_navigation(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    links = [LinkInfo(kind="javascript", text="Menü", in_navigation=False)]
    raw = _update_all(raw_audit_full, with_website, links=links)
    outcome, measured = _run("b5", raw)
    assert outcome is Outcome.PASSED
    assert measured["navigation_measured"] is False


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"meta_robots_bing": "noarchive"}, Outcome.PARTIAL),
        ({"meta_robots_bing": "nocache"}, Outcome.PARTIAL),
        ({"meta_robots_bing": "nosnippet"}, Outcome.PARTIAL),
        ({"meta_robots_bing": "max-snippet:0"}, Outcome.PARTIAL),
        ({"meta_robots_bing": "max-snippet:60"}, Outcome.PASSED),
        ({"meta_robots_bing": "bingbot: noarchive"}, Outcome.PARTIAL),
        ({"meta_robots_bing": "googlebot: noarchive"}, Outcome.PASSED),
        ({"x_robots_tag": "bingbot: nosnippet"}, Outcome.PARTIAL),
        ({"x_robots_tag": "googlebot: nosnippet"}, Outcome.PASSED),
        ({"has_data_nosnippet": True}, Outcome.PARTIAL),
        ({"meta_robots_bing": "noindex"}, Outcome.PASSED),
    ],
)
def test_b10_copilot_blocks_on_a_subpage(
    raw_audit_full: RawAudit, with_website: Edit, update: dict[str, Any], expected: CheckOutcome
) -> None:
    raw = _update_url(raw_audit_full, with_website, SUBPAGE, **update)
    assert _run("b10", raw)[0] is expected


def test_b10_a_block_on_the_start_page_fails_and_names_the_reason(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(raw_audit_full, with_website, START, meta_robots_bing="noarchive, nocache")
    outcome, measured = _run("b10", raw)
    assert outcome is Outcome.FAILED
    assert measured["pages_affected_reasons"] == {START: ["noarchive", "nocache"]}


def _sitemap_probes(statuses: list[int | None]) -> list[UrlProbe]:
    return [
        UrlProbe(
            url=f"{SITE}/s{index}/",
            status=status,
            chain=[status] if status else [],
            error=None if status else "ReadTimeout",
        )
        for index, status in enumerate(statuses)
    ]


def _entries(lastmods: list[str | None]) -> list[SitemapEntry]:
    return [
        SitemapEntry(url=f"{SITE}/s{index}/", lastmod=lastmod)
        for index, lastmod in enumerate(lastmods)
    ]


SEVEN = 7


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        (0, Outcome.PASSED),
        (1, Outcome.PARTIAL),
        (3, Outcome.PARTIAL),
        (4, Outcome.PARTIAL),
        (SEVEN, Outcome.PARTIAL),
    ],
)
def test_b11_share_of_sitemap_entries_that_do_not_answer_with_200(
    raw_audit_full: RawAudit, with_website: Edit, broken: int, expected: CheckOutcome
) -> None:
    statuses: list[int | None] = [404] * broken + [200] * (SEVEN - broken)
    raw = with_website(
        raw_audit_full,
        sitemap_entries=_entries(["2026-09-01"] * SEVEN),
        sitemap_probes=_sitemap_probes(statuses),
    )
    outcome, measured = _run("b11", raw)
    assert outcome is expected
    assert measured["entries_with_problem_count"] == broken
    assert measured["entries_checked"] == SEVEN


@pytest.mark.parametrize(
    ("with_date", "expected"),
    [
        (SEVEN, Outcome.PASSED),
        (4, Outcome.PARTIAL),
        (3, Outcome.PARTIAL),
        (0, Outcome.PARTIAL),
    ],
)
def test_b11_share_of_sitemap_entries_with_a_change_date(
    raw_audit_full: RawAudit, with_website: Edit, with_date: int, expected: CheckOutcome
) -> None:
    lastmods: list[str | None] = ["2026-09-01"] * with_date + [None] * (SEVEN - with_date)
    raw = with_website(
        raw_audit_full,
        sitemap_entries=_entries(lastmods),
        sitemap_probes=_sitemap_probes([200] * SEVEN),
    )
    outcome, measured = _run("b11", raw)
    assert outcome is expected
    assert measured["entries_with_lastmod"] == with_date


def test_b11_an_entry_for_the_start_page_that_does_not_answer_fails(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    entries = [SitemapEntry(url=START, lastmod="2026-09-01")]
    probes = [UrlProbe(url=START, status=404, chain=[404])]
    raw = with_website(raw_audit_full, sitemap_entries=entries, sitemap_probes=probes)
    assert _run("b11", raw)[0] is Outcome.FAILED


def test_b11_an_entry_that_could_not_be_fetched_is_named(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(
        raw_audit_full,
        sitemap_entries=_entries(["2026-09-01"] * 2),
        sitemap_probes=_sitemap_probes([200, None]),
    )
    outcome, measured = _run("b11", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["entries_with_problem"] == {f"{SITE}/s1/": "nicht abrufbar: ReadTimeout"}


def test_b11_an_entry_whose_page_points_to_another_canonical_is_a_problem(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full, with_website, f"{SITE}/kontakt/", canonical=f"{SITE}/anfahrt/"
    )
    outcome, measured = _run("b11", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["entries_with_problem"] == {
        f"{SITE}/kontakt/": f"canonical zeigt auf {SITE}/anfahrt/"
    }


def test_b11_without_any_sitemap_fails(raw_audit_full: RawAudit, with_website: Edit) -> None:
    raw = with_website(raw_audit_full, sitemap_urls=[], sitemap_entries=None)
    assert _run("b11", raw) == (Outcome.FAILED, {"sitemap_found": False})


def test_b11_a_sitemap_that_could_not_be_read_is_not_measured(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, sitemap_entries=[])
    outcome, measured = _run("b11", raw)
    assert outcome is Outcome.NOT_MEASURABLE
    assert measured["reason"] == "sitemap_not_readable"


def test_b11_without_probes_is_not_measured(raw_audit_full: RawAudit, with_website: Edit) -> None:
    raw = with_website(raw_audit_full, sitemap_probes=None)
    assert _run("b11", raw) == (Outcome.NOT_MEASURABLE, {"reason": "no_sitemap_probes"})


def _links(good: int, bad: list[LinkInfo]) -> list[LinkInfo]:
    return [
        *(
            LinkInfo(kind="href", text=f"Seite {index}", url=f"{SITE}/s{index}/")
            for index in range(good)
        ),
        *bad,
    ]


NO_ADDRESS = LinkInfo(kind="javascript", text="Menü")
NO_LABEL = LinkInfo(kind="href", text="", url=f"{SITE}/ohne-text/")


@pytest.mark.parametrize(
    ("good", "bad", "expected"),
    [
        (4, [], Outcome.PASSED),
        (3, [NO_ADDRESS], Outcome.FAILED),
        (2, [NO_ADDRESS, NO_LABEL], Outcome.FAILED),
        (1, [NO_ADDRESS, NO_LABEL, LinkInfo(kind="onclick", text="Los")], Outcome.FAILED),
        (0, [NO_ADDRESS], Outcome.FAILED),
    ],
)
def test_b13_share_of_links_with_an_address_and_a_label(
    raw_audit_full: RawAudit,
    with_website: Edit,
    good: int,
    bad: list[LinkInfo],
    expected: CheckOutcome,
) -> None:
    raw = _update_all(raw_audit_full, with_website, links=_links(good, bad))
    outcome, measured = _run("b13", raw)
    assert outcome is expected
    assert measured["links_total"] == good + len(bad)


def test_b13_links_without_an_address_on_a_subpage_only_are_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(raw_audit_full, with_website, SUBPAGE, links=_links(3, [NO_ADDRESS]))
    outcome, measured = _run("b13", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["pages_with_issues"] == [SUBPAGE]


def test_b13_an_image_link_or_a_label_for_screen_readers_counts_as_labelled(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    links = [
        LinkInfo(kind="href", text="", url=f"{SITE}/a/", image_alt="Logo"),
        LinkInfo(kind="href", text="", url=f"{SITE}/b/", aria_label="Zur Startseite"),
    ]
    raw = _update_all(raw_audit_full, with_website, links=links)
    assert _run("b13", raw)[0] is Outcome.PASSED


def test_b13_anchor_links_are_left_out(raw_audit_full: RawAudit, with_website: Edit) -> None:
    links = [LinkInfo(kind="anchor", text="", url=f"{SITE}/#oben"), *_links(1, [])]
    raw = _update_all(raw_audit_full, with_website, links=links)
    outcome, measured = _run("b13", raw)
    assert outcome is Outcome.PASSED
    assert measured["links_total"] == 1


def test_b13_without_any_link_is_not_applicable(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, links=[])
    assert _run("b13", raw) == (Outcome.NOT_APPLICABLE, {"reason": "no_links"})


def _probe(url: str, status: int | None, *chain: int) -> UrlProbe:
    return UrlProbe(url=url, status=status, chain=list(chain) or ([status] if status else []))


def test_b14_all_variants_end_on_one_address(raw_audit_full: RawAudit) -> None:
    outcome, measured = _run("b14", raw_audit_full)
    assert outcome is Outcome.PASSED
    assert measured["duplicate_addresses"] == []
    assert measured["variants_checked"] == 6


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (_probe("http://dachdeckerei-muster.de/", 200), Outcome.FAILED),
        (_probe("https://dachdeckerei-muster.de/", 200), Outcome.FAILED),
        (_probe(f"{SITE}/dachsanierung", 200), Outcome.PARTIAL),
        (_probe("http://www.dachdeckerei-muster.de/", 302, 302, 200), Outcome.PARTIAL),
        (_probe("http://www.dachdeckerei-muster.de/", 301, 301, 200), Outcome.PASSED),
        (_probe("http://www.dachdeckerei-muster.de/", 307, 307, 200), Outcome.PARTIAL),
        (_probe("http://www.dachdeckerei-muster.de/", 404), Outcome.PASSED),
    ],
)
def test_b14_one_variant_that_stays_reachable(
    raw_audit_full: RawAudit, with_website: Edit, variant: UrlProbe, expected: CheckOutcome
) -> None:
    assert raw_audit_full.website is not None
    kept = [
        probe for probe in raw_audit_full.website.variant_probes or [] if probe.url != variant.url
    ]
    raw = with_website(raw_audit_full, variant_probes=[*kept, variant])
    assert _run("b14", raw)[0] is expected


def test_b14_variants_that_did_not_answer_are_listed_but_do_not_count(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    gone = UrlProbe(url="http://dachdeckerei-muster.de/", status=None, error="ConnectError")
    assert raw_audit_full.website is not None
    kept = [probe for probe in raw_audit_full.website.variant_probes or [] if probe.url != gone.url]
    outcome, measured = _run("b14", with_website(raw_audit_full, variant_probes=[*kept, gone]))
    assert outcome is Outcome.PASSED
    assert measured["unreachable_variants"] == ["http://dachdeckerei-muster.de/"]


def test_b14_without_variants_or_without_answers_it_is_not_measured(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    assert _run("b14", with_website(raw_audit_full, variant_probes=None))[1] == {
        "reason": "no_variant_probes"
    }
    silent = [UrlProbe(url="http://dachdeckerei-muster.de/", status=None, error="ConnectError")]
    assert _run("b14", with_website(raw_audit_full, variant_probes=silent))[1] == {
        "reason": "no_variant_answered"
    }


def _words(count: int) -> str:
    return " ".join(f"wort{index}" for index in range(count))


@pytest.mark.parametrize(
    ("words", "scripts", "expected"),
    [
        (0, True, Outcome.FAILED),
        (1, True, Outcome.PASSED),
        (29, True, Outcome.PASSED),
        (0, False, Outcome.PASSED),
    ],
)
def test_b15_a_start_page_without_any_text_before_the_scripts_run_fails(
    raw_audit_full: RawAudit,
    with_website: Edit,
    words: int,
    scripts: bool,
    expected: CheckOutcome,
) -> None:
    raw = _update_url(
        raw_audit_full,
        with_website,
        START,
        body_text=_words(words),
        script_sources=[f"{SITE}/app.js"] if scripts else None,
    )
    outcome, measured = _run("b15", raw)
    assert outcome is expected
    assert "min_words" not in measured


def test_b15_a_subpage_without_text_that_needs_scripts_is_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full, with_website, SUBPAGE, body_text="", script_sources=[f"{SITE}/app.js"]
    )
    outcome, measured = _run("b15", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["pages_without_text"] == [SUBPAGE]


def test_b15_a_short_page_without_scripts_is_no_finding(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full, with_website, SUBPAGE, body_text=_words(3), script_sources=None
    )
    assert _run("b15", raw)[0] is Outcome.PASSED


def _images(
    file_names: list[str | None], alt: str | None = "Dachdecker bei der Arbeit"
) -> list[ImageInfo]:
    return [
        ImageInfo(source=f"{SITE}/b/{name}" if name else None, file_name=name, alt=alt)
        for name in file_names
    ]


@pytest.mark.parametrize(("broken", "expected"), SHARE_BOUNDARY)
def test_b16_share_of_images_with_a_generic_file_name(
    raw_audit_full: RawAudit, with_website: Edit, broken: int, expected: CheckOutcome
) -> None:
    first_broken = CONTENT_PAGES - broken
    raw = _map_pages(
        raw_audit_full,
        with_website,
        lambda index, page: page.model_copy(
            update={
                "images": _images(
                    ["IMG_4711.jpg" if first_broken <= index < CONTENT_PAGES else "dachdecker.jpg"]
                )
            }
        ),
    )
    outcome, measured = _run("b16", raw)
    assert outcome is expected
    assert measured["images_with_generic_name"] == broken


def test_b16_an_image_without_a_description_is_flawed_once(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _map_pages(
        raw_audit_full,
        with_website,
        lambda index, page: page.model_copy(
            update={
                "images": [ImageInfo(source=f"{SITE}/b/IMG_1.jpg", file_name="IMG_1.jpg", alt="")]
            }
        ),
    )
    outcome, measured = _run("b16", raw)
    assert outcome is Outcome.FAILED
    assert measured["images_total"] == CONTENT_PAGES
    assert measured["images_without_alt"] == CONTENT_PAGES
    assert measured["images_with_generic_name"] == CONTENT_PAGES


def test_b16_decorative_images_and_images_without_a_file_name_are_left_out(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    decorative = [ImageInfo(source=None, file_name="IMG_1.jpg", alt="", decorative=True)]
    raw = _update_all(raw_audit_full, with_website, images=decorative)
    assert _run("b16", raw) == (Outcome.NOT_APPLICABLE, {"reason": "no_content_images"})
    inline = [ImageInfo(source=None, file_name=None, alt="Ein Bild")]
    assert (
        _run("b16", _update_all(raw_audit_full, with_website, images=inline))[0] is Outcome.PASSED
    )


@pytest.mark.parametrize(
    ("file_name", "descriptive"),
    [
        ("dachdecker-muenster.jpg", True),
        ("Dachfenster-Velux_1.webp", True),
        ("logo.png", True),
        ("steildach~mv2.jpg", True),
        ("IMG_0042.JPG", False),
        ("DSC01234.jpg", False),
        ("image.png", False),
        ("bild-1.jpg", False),
        ("a1b2c3d4e5f6.png", False),
        ("5a2f.png", False),
        ("2024.jpg", False),
        ("screenshot_12.png", False),
    ],
)
def test_descriptive_file_names(file_name: str, descriptive: bool) -> None:
    assert is_descriptive_filename(file_name) is descriptive


def _break_topic(raw: RawAudit, with_website: Edit, urls: list[str], **updates: Any) -> RawAudit:
    for url in urls:
        raw = _update_url(raw, with_website, url, **updates)
    return raw


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        ([], Outcome.PASSED),
        ([START], Outcome.FAILED),
        ([SERVICE_PAGE], Outcome.PARTIAL),
        ([START, SERVICE_PAGE], Outcome.FAILED),
    ],
)
def test_b17_one_topic_per_page(
    raw_audit_full: RawAudit, with_website: Edit, urls: list[str], expected: CheckOutcome
) -> None:
    raw = _break_topic(raw_audit_full, with_website, urls, title="Zzzz yyyy xxxx")
    outcome, measured = _run("b17", raw)
    assert outcome is expected
    assert measured["pages_total"] == 2
    assert set(measured["problems_by_page"].values()) <= {"no_common_topic"}


def test_b17_a_page_without_a_main_heading_or_a_title_is_named(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(raw_audit_full, with_website, START, headings=[(2, "Unter")])
    raw = _update_url(raw, with_website, SERVICE_PAGE, title=None)
    outcome, measured = _run("b17", raw)
    assert outcome is Outcome.FAILED
    assert measured["problems_by_page"] == {START: "no_h1", SERVICE_PAGE: "no_title"}


def test_b17_the_city_alone_does_not_count_as_a_topic(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full,
        with_website,
        START,
        title="Münster",
        headings=[(1, "Münster")],
        body_text="Münster Münster Münster",
    )
    assert _run("b17", raw)[1]["problems_by_page"] == {START: "no_common_topic"}


def test_b17_a_trade_that_stands_in_the_business_name_still_counts_as_the_topic(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    named = raw_audit_full.model_copy(
        update={"business": BusinessFacts(name="Malerbetrieb Schmidt", city="Münster")}
    )
    raw = _update_url(
        named,
        with_website,
        START,
        title="Malerbetrieb in Münster",
        headings=[(1, "Malerbetrieb in Münster")],
        body_text="Malerbetrieb in Münster " + _words(40),
    )
    outcome, measured = _run("b17", raw)
    assert measured["problems_by_page"].get(START) != "no_common_topic"
    assert outcome is Outcome.PASSED


def test_b17_without_content_pages_that_carry_a_topic_it_is_not_measured(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    assert raw_audit_full.website is not None
    only_contact = [
        page for page in raw_audit_full.website.pages if str(page.url).endswith("/kontakt/")
    ]
    raw = with_website(raw_audit_full, pages=only_contact)
    assert _run("b17", raw) == (Outcome.NOT_MEASURABLE, {"reason": "no_topic_pages"})


def _no_serps(raw: RawAudit) -> RawAudit:
    return raw.model_copy(update={"serps": [], "map_rankings": []})


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        ([], Outcome.PASSED),
        ([START], Outcome.FAILED),
        ([SERVICE_PAGE], Outcome.PARTIAL),
        ([START, SERVICE_PAGE], Outcome.FAILED),
    ],
)
def test_b18_service_and_city_at_the_top_of_the_page(
    raw_audit_full: RawAudit, with_website: Edit, urls: list[str], expected: CheckOutcome
) -> None:
    raw = _break_topic(
        raw_audit_full,
        with_website,
        urls,
        first_text="Herzlich willkommen auf unserer Seite",
        headings=[(1, "Willkommen")],
    )
    outcome, measured = _run("b18", raw)
    assert outcome is expected
    assert measured["pages_total"] == 2
    assert measured["city"] == "Münster"


def test_b18_the_city_without_the_service_is_not_enough(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full,
        with_website,
        START,
        first_text="Willkommen in Münster",
        headings=[(1, "Willkommen in Münster")],
    )
    assert _run("b18", raw)[1]["pages_without_service_and_city"] == [START]


def test_b18_is_not_measured_without_search_phrases_or_city(
    raw_audit_full: RawAudit,
) -> None:
    assert _run("b18", _no_serps(raw_audit_full))[1] == {"reason": "no_search_phrases"}
    no_city = raw_audit_full.model_copy(update={"business": BusinessFacts(name="Muster")})
    assert _run("b18", no_city)[1] == {"reason": "no_city"}


COLLECTED_YEAR_AGO_FRESH = "2025-10-01T12:00:00+00:00"
COLLECTED_YEAR_AGO_STALE = "2025-09-30T12:00:00+00:00"


def test_b19_any_change_date_passes_and_the_newest_is_reported(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, last_modified="2019-03-01T12:00:00+00:00")
    raw = _update_url(raw, with_website, SUBPAGE, last_modified="2024-05-02T08:00:00+00:00")
    outcome, measured = _run("b19", raw)
    assert outcome is Outcome.PASSED
    assert measured["newest_change_date"] == "2024-05-02T08:00:00+00:00"
    assert measured["pages_dated"] == CONTENT_PAGES
    assert "max_age_days" not in measured


def test_b19_a_date_from_the_sitemap_stands_in_for_a_missing_header(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, last_modified=None)
    raw = with_website(
        raw,
        sitemap_entries=[SitemapEntry(url=SUBPAGE, lastmod="2023-01-01")],
    )
    outcome, measured = _run("b19", raw)
    assert outcome is Outcome.PASSED
    assert measured["pages_dated"] == 1
    assert measured["newest_change_date"] == "2023-01-01"


def test_b19_without_any_date_it_is_not_measured(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, last_modified=None)
    raw = with_website(raw, sitemap_entries=None)
    assert _run("b19", raw) == (Outcome.NOT_MEASURABLE, {"reason": "no_dates"})


def test_b19_a_date_that_cannot_be_read_does_not_count(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, last_modified="gestern")
    raw = with_website(raw, sitemap_entries=None)
    assert _run("b19", raw)[1] == {"reason": "no_dates"}


def _schema(name: str, phone: str, street: str, postal: str, city: str) -> list[dict[str, object]]:
    return [
        {
            "@context": "https://schema.org",
            "@type": "RoofingContractor",
            "name": name,
            "telephone": phone,
            "address": {
                "@type": "PostalAddress",
                "streetAddress": street,
                "postalCode": postal,
                "addressLocality": city,
            },
        }
    ]


RIGHT = ("Dachdeckerei Muster GmbH", "+49 251 123456", "Hauptstraße 5", "48143", "Münster")


@pytest.mark.parametrize(
    ("wrong", "expected"),
    [
        (0, Outcome.PASSED),
        (1, Outcome.FAILED),
        (2, Outcome.FAILED),
        (3, Outcome.FAILED),
        (5, Outcome.FAILED),
    ],
)
def test_b20_share_of_structured_data_values_that_the_page_text_confirms(
    raw_audit_full: RawAudit, with_website: Edit, wrong: int, expected: CheckOutcome
) -> None:
    values = ["Anderer Betrieb", "+49 999 7654321", "Zwischenweg 99", "99999", "Hamburg"]
    claimed = [values[index] if index < wrong else RIGHT[index] for index in range(5)]
    raw = _update_url(raw_audit_full, with_website, START, json_ld=_schema(*claimed))
    outcome, measured = _run("b20", raw)
    assert outcome is expected
    assert measured["values_checked"] == 5
    assert measured["mismatch_count"] == wrong


def test_b20_a_street_with_another_spelling_is_confirmed(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    claimed = [RIGHT[0], RIGHT[1], "Hauptstrasse 5", RIGHT[3], RIGHT[4]]
    raw = _update_url(raw_audit_full, with_website, START, json_ld=_schema(*claimed))
    assert _run("b20", raw)[0] is Outcome.PASSED


def test_b20_without_structured_data_about_the_business_it_is_not_applicable(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_all(raw_audit_full, with_website, json_ld=[])
    assert _run("b20", raw) == (Outcome.NOT_APPLICABLE, {"reason": "no_structured_data"})


@pytest.mark.parametrize(
    ("footer", "schema", "title", "expected"),
    [
        (
            "Dachdeckerei Muster GmbH",
            "Dachdeckerei Muster",
            "Start | Dachdeckerei Muster",
            Outcome.PASSED,
        ),
        ("Andere Firma", "Dachdeckerei Muster", "Dachdeckerei Muster", Outcome.PARTIAL),
        ("Dachdeckerei Muster", "Andere Firma", "Dachdeckerei Muster", Outcome.PARTIAL),
        ("Dachdeckerei Muster", "Dachdeckerei Muster", "Startseite", Outcome.PARTIAL),
        ("Andere Firma", "Dritte Firma", "Startseite", Outcome.FAILED),
    ],
)
def test_b21_the_name_in_footer_structured_data_and_title(
    raw_audit_full: RawAudit,
    with_website: Edit,
    footer: str,
    schema: str,
    title: str,
    expected: CheckOutcome,
) -> None:
    raw = with_website(raw_audit_full, footer_nap={"name": footer})
    raw = _update_url(
        raw,
        with_website,
        START,
        title=title,
        json_ld=_schema(schema, RIGHT[1], RIGHT[2], RIGHT[3], RIGHT[4]),
    )
    outcome, measured = _run("b21", raw)
    assert outcome is expected
    assert measured["business_name"] == "Dachdeckerei Muster GmbH"
    assert set(measured["sources"]) == {"footer", "structured_data", "title"}


def test_b21_the_site_name_of_the_share_tags_is_a_source_too(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _update_url(
        raw_audit_full, with_website, START, open_graph={"og:site_name": "Muster Dachbau"}
    )
    outcome, measured = _run("b21", raw)
    assert outcome is Outcome.PARTIAL
    assert measured["mismatching_sources"] == ["site_name"]


def test_b21_without_any_name_on_the_site_it_is_not_measured(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, footer_nap={})
    raw = _update_all(raw, with_website, title=None, json_ld=[], open_graph={})
    assert _run("b21", raw) == (Outcome.NOT_MEASURABLE, {"reason": "no_name_sources"})


def test_b22_the_visitor_and_bingbot_get_the_same_page(raw_audit_full: RawAudit) -> None:
    assert _run("b22", raw_audit_full)[0] is Outcome.PASSED


def test_a_threshold_that_is_missing_stops_the_check(raw_audit_full: RawAudit) -> None:
    with pytest.raises(ScoringError):
        run("b22", raw_audit_full, {})
