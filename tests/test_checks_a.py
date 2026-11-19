from collections.abc import Callable
from typing import Any

import pytest

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.errors import ScoringError
from radarauditor.scoring.checks.a_technik import run

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "a2": {
        "lcp_good_ms": 2500,
        "lcp_poor_ms": 4000,
        "cls_good": 0.1,
        "cls_poor": 0.25,
        "inp_good_ms": 200,
        "inp_poor_ms": 500,
        "tbt_good_ms": 200,
        "tbt_poor_ms": 600,
    },
    "a4": {},
    "a7": {},
}
Edit = Callable[..., RawAudit]


def _speed(raw: RawAudit, **updates: Any) -> RawAudit:
    assert raw.pagespeed is not None
    return raw.model_copy(update={"pagespeed": raw.pagespeed.model_copy(update=updates)})


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    outcome, _ = run(check_id, raw_audit_full, THRESHOLDS[check_id])
    assert outcome is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    outcome, measured = run(check_id, raw_audit_minimal, THRESHOLDS[check_id])
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] in {"no_website", "no_pagespeed"}


def test_unknown_check_raises(raw_audit_full: RawAudit) -> None:
    with pytest.raises(ScoringError):
        run("a99", raw_audit_full, {})


def test_missing_threshold_raises(raw_audit_full: RawAudit) -> None:
    with pytest.raises(ScoringError, match="lcp_good_ms"):
        run("a2", raw_audit_full, {})


def _lab_only(raw: RawAudit, **lab: Any) -> RawAudit:
    assert raw.pagespeed is not None and raw.pagespeed.lab is not None
    return _speed(raw, field_data=None, lab=raw.pagespeed.lab.model_copy(update=lab))


def _visitors(raw: RawAudit, **values: Any) -> RawAudit:
    assert raw.pagespeed is not None and raw.pagespeed.field_data is not None
    return _speed(raw, field_data=raw.pagespeed.field_data.model_copy(update=values))


def test_a2_reports_every_metric_with_source_and_rating(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("a2", raw_audit_full, THRESHOLDS["a2"])
    assert outcome is CheckOutcome.PASSED
    assert measured == {
        "lcp_ms": 2100,
        "lcp_source": "field_url",
        "lcp_rating": "good",
        "cls": 0.05,
        "cls_source": "field_url",
        "cls_rating": "good",
        "inp_ms": 150,
        "inp_source": "field_url",
        "inp_rating": "good",
        "lab_runs": 5,
        "mobile_performance": 85,
        "desktop_performance": 97,
    }


def test_a2_prefers_visitor_values_over_the_lab(raw_audit_full: RawAudit) -> None:
    assert raw_audit_full.pagespeed is not None and raw_audit_full.pagespeed.lab is not None
    slow_lab = raw_audit_full.pagespeed.lab.model_copy(update={"lcp_ms": 6000, "cls": 0.4})
    outcome, measured = run("a2", _speed(raw_audit_full, lab=slow_lab), THRESHOLDS["a2"])
    assert outcome is CheckOutcome.PASSED
    assert measured["lcp_ms"] == 2100


def test_a2_names_domain_visitor_data(raw_audit_full: RawAudit) -> None:
    _, measured = run("a2", _visitors(raw_audit_full, scope="origin"), THRESHOLDS["a2"])
    assert measured["lcp_source"] == "field_origin"


def test_a2_fills_missing_visitor_metrics_from_the_lab(raw_audit_full: RawAudit) -> None:
    raw = _visitors(raw_audit_full, cls=None, inp_ms=None)
    outcome, measured = run("a2", raw, THRESHOLDS["a2"])
    assert outcome is CheckOutcome.PASSED
    assert (measured["lcp_source"], measured["cls_source"]) == ("field_url", "lab")
    assert (measured["tbt_ms"], measured["tbt_source"]) == (150, "lab")
    assert "inp_ms" not in measured


def test_a2_without_visitor_data_judges_the_lab(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("a2", _lab_only(raw_audit_full), THRESHOLDS["a2"])
    assert outcome is CheckOutcome.PASSED
    assert {measured["lcp_source"], measured["cls_source"], measured["tbt_source"]} == {"lab"}


def test_a2_ignores_the_overall_mobile_score(raw_audit_full: RawAudit) -> None:
    raw = _speed(raw_audit_full, mobile_performance=10)
    assert run("a2", raw, THRESHOLDS["a2"])[0] is CheckOutcome.PASSED


def test_a2_without_lab_runs_is_not_measurable(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("a2", _speed(raw_audit_full, lab=None), THRESHOLDS["a2"])
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] == "no_pagespeed"


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("lcp_ms", 2500, CheckOutcome.PASSED),
        ("lcp_ms", 2501, CheckOutcome.PARTIAL),
        ("lcp_ms", 4000, CheckOutcome.PARTIAL),
        ("lcp_ms", 4001, CheckOutcome.FAILED),
        ("cls", 0.1, CheckOutcome.PASSED),
        ("cls", 0.101, CheckOutcome.PARTIAL),
        ("cls", 0.25, CheckOutcome.PARTIAL),
        ("cls", 0.251, CheckOutcome.FAILED),
        ("tbt_ms", 200, CheckOutcome.PASSED),
        ("tbt_ms", 201, CheckOutcome.PARTIAL),
        ("tbt_ms", 600, CheckOutcome.PARTIAL),
        ("tbt_ms", 601, CheckOutcome.FAILED),
    ],
)
def test_a2_lab_limits(
    raw_audit_full: RawAudit, metric: str, value: float, expected: CheckOutcome
) -> None:
    assert run("a2", _lab_only(raw_audit_full, **{metric: value}), THRESHOLDS["a2"])[0] is expected


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("lcp_ms", 2501, CheckOutcome.PARTIAL),
        ("lcp_ms", 4001, CheckOutcome.FAILED),
        ("cls", 0.101, CheckOutcome.PARTIAL),
        ("cls", 0.251, CheckOutcome.FAILED),
        ("inp_ms", 200, CheckOutcome.PASSED),
        ("inp_ms", 201, CheckOutcome.PARTIAL),
        ("inp_ms", 500, CheckOutcome.PARTIAL),
        ("inp_ms", 501, CheckOutcome.FAILED),
    ],
)
def test_a2_visitor_limits(
    raw_audit_full: RawAudit, metric: str, value: float, expected: CheckOutcome
) -> None:
    raw = _visitors(raw_audit_full, **{metric: value})
    assert run("a2", raw, THRESHOLDS["a2"])[0] is expected


def test_a2_rates_each_metric(raw_audit_full: RawAudit) -> None:
    raw = _visitors(raw_audit_full, lcp_ms=3000, inp_ms=600)
    outcome, measured = run("a2", raw, THRESHOLDS["a2"])
    assert outcome is CheckOutcome.FAILED
    ratings = (measured["lcp_rating"], measured["cls_rating"], measured["inp_rating"])
    assert ratings == ("needs_improvement", "good", "poor")


def _with_page(raw: RawAudit, with_website: Edit, index: int, **updates: Any) -> RawAudit:
    assert raw.website is not None
    pages = list(raw.website.pages)
    pages[index] = pages[index].model_copy(update=updates)
    return with_website(raw, pages=pages)


START, SUBPAGE, LINKED_LEGAL_PAGE = 0, 3, 4


def test_a4_robots_blocking_everything_fails(raw_audit_full: RawAudit, with_website: Edit) -> None:
    blocked = with_website(raw_audit_full, robots_txt="User-agent: *\nDisallow: /\n")
    outcome, measured = run("a4", blocked, {})
    assert outcome is CheckOutcome.FAILED
    assert measured["start_page_affected"] is True


@pytest.mark.parametrize(
    ("robots", "expected"),
    [
        ("User-agent: *\nDisallow: /ueber-uns/\n", CheckOutcome.PARTIAL),
        ("User-agent: Googlebot\nDisallow: /ueber-uns/\n", CheckOutcome.PARTIAL),
        ("User-agent: Bingbot\nDisallow: /\n", CheckOutcome.PASSED),
        ("User-agent: *\nDisallow:\n", CheckOutcome.PASSED),
        (None, CheckOutcome.NOT_MEASURABLE),
    ],
)
def test_a4_robots_rules_count_for_googlebot_only(
    raw_audit_full: RawAudit, with_website: Edit, robots: str | None, expected: CheckOutcome
) -> None:
    raw = with_website(raw_audit_full, robots_txt=robots)
    assert run("a4", raw, {})[0] is expected


def test_a4_a_blocked_subpage_is_listed(raw_audit_full: RawAudit, with_website: Edit) -> None:
    raw = with_website(raw_audit_full, robots_txt="User-agent: *\nDisallow: /ueber-uns/\n")
    measured = run("a4", raw, {})[1]
    assert measured["blocked_by_robots"] == ["https://www.dachdeckerei-muster.de/ueber-uns/"]
    assert measured["start_page_affected"] is False


def test_a4_without_sitemap_is_no_defect(raw_audit_full: RawAudit, with_website: Edit) -> None:
    assert run("a4", with_website(raw_audit_full, sitemap_urls=[]), {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize(
    ("meta", "header", "expected"),
    [
        ("noindex", None, CheckOutcome.PARTIAL),
        ("none", None, CheckOutcome.PARTIAL),
        (None, "noindex", CheckOutcome.PARTIAL),
        ("googlebot: noindex", None, CheckOutcome.PARTIAL),
        ("bingbot: noindex", None, CheckOutcome.PASSED),
        ("index, follow", None, CheckOutcome.PASSED),
        ("max-snippet:0", None, CheckOutcome.PASSED),
    ],
)
def test_a4_noindex_on_a_subpage(
    raw_audit_full: RawAudit,
    with_website: Edit,
    meta: str | None,
    header: str | None,
    expected: CheckOutcome,
) -> None:
    raw = _with_page(raw_audit_full, with_website, SUBPAGE, meta_robots=meta, x_robots_tag=header)
    assert run("a4", raw, {})[0] is expected


def test_a4_noindex_on_the_start_page_fails(raw_audit_full: RawAudit, with_website: Edit) -> None:
    raw = _with_page(raw_audit_full, with_website, START, meta_robots="noindex")
    outcome, measured = run("a4", raw, {})
    assert outcome is CheckOutcome.FAILED
    assert measured["start_page_affected"] is True


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        ("https://www.dachdeckerei-muster.de/ueber-uns/", CheckOutcome.PASSED),
        ("https://www.dachdeckerei-muster.de/ueber-uns", CheckOutcome.PASSED),
        ("https://www.dachdeckerei-muster.de/ueber-uns/#team", CheckOutcome.PASSED),
        ("https://www.dachdeckerei-muster.de/team/", CheckOutcome.PARTIAL),
    ],
)
def test_a4_canonical_pointing_elsewhere(
    raw_audit_full: RawAudit, with_website: Edit, canonical: str, expected: CheckOutcome
) -> None:
    raw = _with_page(raw_audit_full, with_website, SUBPAGE, canonical=canonical)
    assert run("a4", raw, {})[0] is expected


def test_a4_a_linked_page_with_an_error_status_is_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _with_page(raw_audit_full, with_website, LINKED_LEGAL_PAGE, status_code=404)
    outcome, measured = run("a4", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["wrong_status"] == ["https://www.dachdeckerei-muster.de/impressum/"]


def test_a4_an_unlinked_page_with_an_error_status_is_no_defect(
    raw_audit_full: RawAudit, with_website: Edit, make_page: Callable[..., Any]
) -> None:
    assert raw_audit_full.website is not None
    orphan = make_page(url="https://www.dachdeckerei-muster.de/versteckt/", status_code=404)
    raw = with_website(raw_audit_full, pages=[*raw_audit_full.website.pages, orphan])
    assert run("a4", raw, {})[0] is CheckOutcome.PASSED


def test_a4_an_error_status_on_the_start_page_fails(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _with_page(raw_audit_full, with_website, START, status_code=500)
    assert run("a4", raw, {})[0] is CheckOutcome.FAILED


CONTENT_PAGES = 12


def _missing_alt_on(raw: RawAudit, with_website: Edit, pages_hit: int) -> RawAudit:
    assert raw.website is not None
    pages = [
        page.model_copy(
            update={
                "images": [
                    image.model_copy(
                        update={"alt": "" if CONTENT_PAGES - pages_hit <= index else image.alt}
                    )
                    for image in page.images or []
                ]
            }
        )
        for index, page in enumerate(raw.website.pages)
    ]
    return with_website(raw, pages=pages)


@pytest.mark.parametrize(
    ("pages_hit", "expected"),
    [
        (0, CheckOutcome.PASSED),
        (1, CheckOutcome.PARTIAL),
        (6, CheckOutcome.PARTIAL),
        (7, CheckOutcome.PARTIAL),
        (11, CheckOutcome.PARTIAL),
        (12, CheckOutcome.FAILED),
    ],
)
def test_a7_the_start_page_decides_between_partial_and_failed(
    raw_audit_full: RawAudit, with_website: Edit, pages_hit: int, expected: CheckOutcome
) -> None:
    raw = _missing_alt_on(raw_audit_full, with_website, pages_hit)
    outcome, measured = run("a7", raw, THRESHOLDS["a7"])
    assert outcome is expected
    assert measured["images_without_alt"] == pages_hit
    assert measured["images_total"] == 12


def test_a2_marks_the_older_flat_pagespeed_format_as_outdated(raw_audit_full: RawAudit) -> None:
    assert raw_audit_full.pagespeed is not None
    older = raw_audit_full.pagespeed.model_dump(mode="json", exclude={"lab", "field_data"})
    older.update({"lcp_ms": 2300, "cls": 0.06, "inp_ms": 150})
    raw = raw_audit_full.model_copy(
        update={"pagespeed": type(raw_audit_full.pagespeed).model_validate(older)}
    )
    outcome, measured = run("a2", raw, THRESHOLDS["a2"])
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] == "pagespeed_data_outdated"


def test_a2_without_lab_values_in_the_new_format_stays_no_pagespeed(
    raw_audit_full: RawAudit,
) -> None:
    outcome, measured = run("a2", _speed(raw_audit_full, lab=None), THRESHOLDS["a2"])
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] == "no_pagespeed"
