from collections.abc import Callable
from typing import Any

import pytest

from radarauditor.collectors.snapshot import PageSnapshot, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.b_struktur import run

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "b5": {},
}
Edit = Callable[..., RawAudit]
SITE = "https://www.dachdeckerei-muster.de"


def _pages(raw: RawAudit) -> list[PageSnapshot]:
    assert raw.website is not None
    return list(raw.website.pages)


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    outcome, measured = run(check_id, raw_audit_minimal, THRESHOLDS[check_id])
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] == "no_website"


def _with_pages(raw: RawAudit, with_website: Edit, extra: list[PageSnapshot]) -> RawAudit:
    return with_website(raw, pages=[*_pages(raw), *extra])


def test_b5_an_unlinked_page_is_listed(
    raw_audit_full: RawAudit, with_website: Edit, make_page: Callable[..., Any]
) -> None:
    orphan = make_page(url=f"{SITE}/versteckt/")
    outcome, measured = run("b5", _with_pages(raw_audit_full, with_website, [orphan]), {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["pages_not_linked"] == [f"{SITE}/versteckt/"]
    assert measured["pages_unreachable_count"] == 1


def test_b5_a_page_linked_by_another_page_is_reachable(
    raw_audit_full: RawAudit, with_website: Edit, make_page: Callable[..., Any]
) -> None:
    other = make_page(url=f"{SITE}/versteckt/")
    linker = make_page(url=f"{SITE}/weg/", internal_links=[f"{SITE}/versteckt/"])
    pages = [*_pages(raw_audit_full), other, linker]
    start = pages[0].model_copy(update={"internal_links": [*pages[0].internal_links, linker.url]})
    raw = with_website(raw_audit_full, pages=[start, *pages[1:]])
    assert run("b5", raw, {})[0] is CheckOutcome.PASSED


def test_b5_a_link_from_the_page_to_itself_does_not_count(
    raw_audit_full: RawAudit, with_website: Edit, make_page: Callable[..., Any]
) -> None:
    selfish = make_page(url=f"{SITE}/versteckt/", internal_links=[f"{SITE}/versteckt/"])
    raw = _with_pages(raw_audit_full, with_website, [selfish])
    assert run("b5", raw, {})[1]["pages_not_linked"] == [f"{SITE}/versteckt/"]


def test_b5_a_page_only_in_the_sitemap_is_unreachable(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, sitemap_pages=[f"{SITE}/nur-sitemap/"])
    outcome, measured = run("b5", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["pages_only_in_sitemap"] == [f"{SITE}/nur-sitemap/"]


def test_b5_sitemap_pages_are_ignored_when_the_crawl_was_capped(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, sitemap_pages=[f"{SITE}/nur-sitemap/"], crawl_capped=True)
    assert run("b5", raw, {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize(
    ("orphans", "expected"), [(12, CheckOutcome.PARTIAL), (13, CheckOutcome.PARTIAL)]
)
def test_b5_unreachable_subpages_are_partial_however_many_there_are(
    raw_audit_full: RawAudit,
    with_website: Edit,
    make_page: Callable[..., Any],
    orphans: int,
    expected: CheckOutcome,
) -> None:
    extra = [make_page(url=f"{SITE}/versteckt-{n}/") for n in range(orphans)]
    raw = _with_pages(raw_audit_full, with_website, extra)
    assert run("b5", raw, {})[0] is expected
