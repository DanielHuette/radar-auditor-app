from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from radarauditor.collectors.snapshot import AgentFetch, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.catalog import find_check, load_catalogue
from radarauditor.scoring.checks.a_technik import run as run_a
from radarauditor.scoring.checks.b_struktur import run as run_b_checks
from radarauditor.scoring.checks.common import CheckReturn, Thresholds

SITE = "https://www.dachdeckerei-muster.de"
START = f"{SITE}/"
SUBPAGE = f"{SITE}/ueber-uns/"
Edit = Callable[..., RawAudit]
BOT_BY_CHECK = {
    "a13": "googlebot",
    "a16": "oai-searchbot",
    "a18": "claude-searchbot",
}
ROBOTS_BLOCK_ALL = "User-agent: {agent}\nDisallow: /\n"
ROBOTS_BLOCK_RATGEBER = "User-agent: {agent}\nDisallow: /ratgeber/\n"
BLOCK_BOTH_CLAUDE = (
    "User-agent: Claude-SearchBot\nDisallow: /\nUser-agent: Claude-User\nDisallow: /\n"
)
WORDS_OF_THE_VISITOR = [f"startwort{number}" for number in range(40)]
CATALOGUE = load_catalogue(Path(__file__).resolve().parent.parent / "config" / "pruefkatalog.yaml")
SAME_PAGE_LIMITS = find_check(CATALOGUE, "a13").thresholds


def run(check_id: str, raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return run_a(check_id, raw, {**SAME_PAGE_LIMITS, **thresholds})


def run_b(check_id: str, raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return run_b_checks(check_id, raw, {**SAME_PAGE_LIMITS, **thresholds})


def _edit_page(raw: RawAudit, with_website: Edit, url: str, **updates: Any) -> RawAudit:
    assert raw.website is not None
    pages = [
        page.model_copy(update=updates) if str(page.url) == url else page
        for page in raw.website.pages
    ]
    return with_website(raw, pages=pages)


def _edit_all_pages(raw: RawAudit, with_website: Edit, **updates: Any) -> RawAudit:
    assert raw.website is not None
    return with_website(raw, pages=[page.model_copy(update=updates) for page in raw.website.pages])


def _fetches(raw: RawAudit, agent: str, **updates: Any) -> list[AgentFetch]:
    assert raw.website is not None
    assert raw.website.agent_fetches is not None
    return [
        fetch.model_copy(update=updates) if fetch.agent == agent else fetch
        for fetch in raw.website.agent_fetches
    ]


def _bot_text(words: list[str]) -> str:
    return " ".join(words)


@pytest.mark.parametrize("check_id", ["a11", "a12", "a13", "a14", "a15", "a16", "a17", "a18"])
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", ["a11", "a12", "a13", "a15", "a16", "a17", "a18"])
def test_minimal_audit_is_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "no_website"},
    )


def test_a14_minimal_audit_is_not_measurable(raw_audit_minimal: RawAudit) -> None:
    assert run("a14", raw_audit_minimal, {})[1] == {"reason": "no_website"}


@pytest.mark.parametrize(
    ("check_id", "agent", "robots_text", "expected"),
    [
        ("a11", "Google-Extended", ROBOTS_BLOCK_ALL, CheckOutcome.FAILED),
        ("a11", "Google-Extended", ROBOTS_BLOCK_RATGEBER, CheckOutcome.PARTIAL),
        ("a11", "Google-Extended", "User-agent: Google-Extended\nAllow: /\n", CheckOutcome.PASSED),
        ("a15", "OAI-SearchBot", ROBOTS_BLOCK_ALL, CheckOutcome.FAILED),
        ("a15", "OAI-SearchBot", ROBOTS_BLOCK_RATGEBER, CheckOutcome.PARTIAL),
    ],
)
def test_robots_txt_decides_whether_an_assistant_may_read_the_site(
    raw_audit_full: RawAudit,
    with_website: Edit,
    check_id: str,
    agent: str,
    robots_text: str,
    expected: CheckOutcome,
) -> None:
    raw = with_website(raw_audit_full, robots_txt=robots_text.format(agent=agent))
    assert run(check_id, raw, {})[0] is expected


@pytest.mark.parametrize(
    "blocked_url",
    [
        f"{SITE}/wp-admin/",
        f"{SITE}/warenkorb/",
        f"{SITE}/suche/?q=dach",
    ],
)
def test_a_system_path_that_robots_txt_blocks_is_no_finding_for_a4(
    raw_audit_full: RawAudit, with_website: Edit, blocked_url: str
) -> None:
    raw = with_website(
        raw_audit_full,
        robots_txt="User-agent: *\nDisallow: /wp-admin/\nDisallow: /warenkorb/\nDisallow: /suche/\n"
        "Disallow: /ratgeber/?\n",
        robots_skipped=[blocked_url],
    )
    outcome, measured = run("a4", raw, {})
    assert outcome is CheckOutcome.PASSED
    assert measured["blocked_by_robots"] == []


@pytest.mark.parametrize(
    "blocked_url",
    [
        f"{SITE}/?page_id=12",
        f"{SITE}/?p=7",
        f"{SITE}/?PAGE_ID=12",
        f"{SITE}/leistungen/?page_id=3",
        f"{SITE}/?lang=en",
        f"{SITE}/shop/?product=12",
        f"{SITE}/ratgeber/?page=2",
    ],
)
def test_a_wordpress_content_address_that_robots_txt_blocks_is_a_finding_for_a4(
    raw_audit_full: RawAudit, with_website: Edit, blocked_url: str
) -> None:
    raw = with_website(
        raw_audit_full, robots_txt="User-agent: *\nDisallow: /*?\n", robots_skipped=[blocked_url]
    )
    outcome, measured = run("a4", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["blocked_by_robots"] == [blocked_url]
    assert measured["system_paths_blocked"] == []


@pytest.mark.parametrize(
    "blocked_url",
    [
        f"{SITE}/?utm_source=newsletter",
        f"{SITE}/?fbclid=abc",
        f"{SITE}/?gclid=abc",
        f"{SITE}/?sessionid=12",
        f"{SITE}/ratgeber/?sort=preis",
        f"{SITE}/?page_id=12&replytocom=3",
    ],
)
def test_an_address_with_a_listed_technical_parameter_is_a_system_path(
    raw_audit_full: RawAudit, with_website: Edit, blocked_url: str
) -> None:
    raw = with_website(
        raw_audit_full, robots_txt="User-agent: *\nDisallow: /*?\n", robots_skipped=[blocked_url]
    )
    outcome, measured = run("a4", raw, {})
    assert outcome is CheckOutcome.PASSED
    assert measured["blocked_by_robots"] == []
    assert measured["system_paths_blocked"] == [blocked_url]


def test_a_blocked_content_page_that_was_only_found_but_not_crawled_is_a_finding_for_a4(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(
        raw_audit_full,
        robots_txt="User-agent: *\nDisallow: /geheimtipps/\n",
        robots_skipped=[f"{SITE}/geheimtipps/dachrinne/"],
    )
    outcome, measured = run("a4", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["blocked_by_robots"] == [f"{SITE}/geheimtipps/dachrinne/"]


def test_a11_does_not_touch_google_search_and_says_so(raw_audit_full: RawAudit) -> None:
    measured = run("a11", raw_audit_full, {})[1]
    assert measured["agent"] == "Google-Extended"
    assert measured["note"] == "betrifft nicht die Google-Suche"


def test_a11_a_rule_for_googlebot_does_not_block_google_extended(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, robots_txt="User-agent: Googlebot\nDisallow: /\n")
    assert run("a11", raw, {})[0] is CheckOutcome.PASSED


def test_a11_the_wildcard_blocks_google_extended_too(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, robots_txt="User-agent: *\nDisallow: /\n")
    assert run("a11", raw, {})[0] is CheckOutcome.FAILED


def test_a15_names_the_hours_a_change_needs_to_take_effect(raw_audit_full: RawAudit) -> None:
    assert run("a15", raw_audit_full, {})[1]["changes_take_effect_hours"] == 24


ROBOTS_CHECKS = ["a4", "a11", "a14", "a15", "a17"]


@pytest.mark.parametrize("check_id", ["a4", "a12", "a14"])
def test_blocking_notes_of_an_older_crawl_are_not_judged_as_absent(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    raw = _edit_all_pages(raw_audit_full, with_website, body_text=None)
    assert run(check_id, raw, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "crawl_data_outdated"},
    )


@pytest.mark.parametrize("check_id", ["a4", "a12"])
def test_one_page_with_extended_data_is_enough_to_judge_the_blocking_notes(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    assert raw_audit_full.website is not None
    pages = [
        page.model_copy(update={"body_text": None if index else "Text"})
        for index, page in enumerate(raw_audit_full.website.pages)
    ]
    raw = with_website(raw_audit_full, pages=pages)
    assert run(check_id, raw, {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 499])
def test_a15_without_robots_txt_everything_is_allowed(
    raw_audit_full: RawAudit, with_website: Edit, status: int
) -> None:
    raw = with_website(raw_audit_full, robots_txt=None, robots_status=status)
    outcome, measured = run("a15", raw, {})
    assert outcome is CheckOutcome.PASSED
    assert measured["robots_txt_present"] is False


@pytest.mark.parametrize("check_id", ROBOTS_CHECKS)
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (500, "robots_txt_server_error"),
        (503, "robots_txt_server_error"),
        (599, "robots_txt_server_error"),
        (429, "robots_txt_rate_limited"),
        (None, "robots_txt_no_answer"),
        (204, "robots_txt_no_answer"),
    ],
)
def test_a_robots_txt_that_could_not_be_read_means_not_measured(
    raw_audit_full: RawAudit,
    with_website: Edit,
    check_id: str,
    status: int | None,
    reason: str,
) -> None:
    raw = with_website(raw_audit_full, robots_txt=None, robots_status=status)
    assert run(check_id, raw, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": reason, "robots_status": status},
    )


@pytest.mark.parametrize("check_id", ROBOTS_CHECKS)
def test_a_robots_txt_that_was_read_counts_whatever_the_stored_status(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    raw = with_website(raw_audit_full, robots_status=None)
    assert run(check_id, raw, {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", ROBOTS_CHECKS)
def test_a_byte_order_mark_does_not_hide_a_block_of_the_whole_site(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    raw = with_website(raw_audit_full, robots_txt="\ufeffUser-agent: *\nDisallow: /\n")
    assert run(check_id, raw, {})[0] is CheckOutcome.FAILED


@pytest.mark.parametrize("check_id", ["a4", "a14"])
def test_a_versioned_user_agent_line_blocks_the_crawler(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    robots_txt = "User-agent: Googlebot/2.1\nUser-agent: Bingbot/2.0\nDisallow: /\n"
    raw = with_website(raw_audit_full, robots_txt=robots_txt)
    assert run(check_id, raw, {})[0] is CheckOutcome.FAILED


@pytest.mark.parametrize(
    ("robots_text", "expected"),
    [
        (BLOCK_BOTH_CLAUDE, "failed"),
        ("User-agent: Claude-SearchBot\nDisallow: /\n", "partial"),
        ("User-agent: Claude-User\nDisallow: /\n", "partial"),
        ("User-agent: Claude-SearchBot\nDisallow: /ratgeber/\n", "partial"),
        ("User-agent: Claude-SearchBot\nAllow: /\n", "passed"),
    ],
)
def test_a17_names_both_claude_identities(
    raw_audit_full: RawAudit, with_website: Edit, robots_text: str, expected: str
) -> None:
    outcome, measured = run("a17", with_website(raw_audit_full, robots_txt=robots_text), {})
    assert outcome is CheckOutcome(expected)
    assert set(measured["agents"]) == {"Claude-SearchBot", "Claude-User"}


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"meta_robots": "nosnippet"}, CheckOutcome.PARTIAL),
        ({"meta_robots": "max-snippet:0"}, CheckOutcome.PARTIAL),
        ({"meta_robots": "max-snippet:50"}, CheckOutcome.PASSED),
        ({"meta_robots": "index, follow"}, CheckOutcome.PASSED),
        ({"x_robots_tag": "googlebot: nosnippet"}, CheckOutcome.PARTIAL),
        ({"x_robots_tag": "bingbot: nosnippet"}, CheckOutcome.PASSED),
        ({"x_robots_tag": "nosnippet"}, CheckOutcome.PARTIAL),
    ],
)
def test_a12_snippet_blocks_on_a_subpage(
    raw_audit_full: RawAudit, with_website: Edit, update: dict[str, str], expected: CheckOutcome
) -> None:
    raw = _edit_page(raw_audit_full, with_website, SUBPAGE, **update)
    assert run("a12", raw, {})[0] is expected


def test_a12_a_snippet_block_on_the_start_page_fails(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _edit_page(raw_audit_full, with_website, START, meta_robots="nosnippet")
    outcome, measured = run("a12", raw, {})
    assert outcome is CheckOutcome.FAILED
    assert measured["pages_affected"] == [START]
    assert measured["pages_affected_reasons"] == {START: "nosnippet"}


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"meta_robots_bing": "noindex"}, CheckOutcome.PARTIAL),
        ({"meta_robots_bing": "none"}, CheckOutcome.PARTIAL),
        ({"meta_robots_bing": "bingbot: noindex"}, CheckOutcome.PARTIAL),
        ({"meta_robots_bing": "googlebot: noindex"}, CheckOutcome.PASSED),
        ({"x_robots_tag": "bingbot: noindex"}, CheckOutcome.PARTIAL),
        ({"meta_robots": "noindex"}, CheckOutcome.PASSED),
        ({"canonical": f"{SITE}/team/"}, CheckOutcome.PARTIAL),
        ({"status_code": 500}, CheckOutcome.PARTIAL),
    ],
)
def test_a14_bing_indexing_on_a_subpage(
    raw_audit_full: RawAudit, with_website: Edit, update: dict[str, Any], expected: CheckOutcome
) -> None:
    raw = _edit_page(raw_audit_full, with_website, SUBPAGE, **update)
    assert run("a14", raw, {})[0] is expected


def test_a14_noindex_on_the_start_page_fails_and_names_the_reason(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _edit_page(raw_audit_full, with_website, START, meta_robots_bing="noindex")
    outcome, measured = run("a14", raw, {})
    assert outcome is CheckOutcome.FAILED
    assert measured["start_page_affected"] is True
    assert measured["pages_affected_reasons"] == {START: "noindex (meta)"}


def test_a14_robots_txt_blocks_bing_only_when_it_names_bing(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    google_only = with_website(raw_audit_full, robots_txt="User-agent: Googlebot\nDisallow: /\n")
    assert run("a14", google_only, {})[0] is CheckOutcome.PASSED
    bing = with_website(raw_audit_full, robots_txt="User-agent: Bingbot\nDisallow: /\n")
    outcome, measured = run("a14", bing, {})
    assert outcome is CheckOutcome.FAILED
    assert START in measured["blocked_by_robots"]


def test_a14_is_not_measured_without_the_bing_meta_values_of_the_new_crawl(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = _edit_all_pages(raw_audit_full, with_website, body_text=None)
    assert run("a14", raw, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "crawl_data_outdated"},
    )


@pytest.mark.parametrize("check_id", sorted(BOT_BY_CHECK))
def test_the_bot_and_the_visitor_get_the_same_page(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize(
    ("shared_words", "foreign_words", "expected"),
    [
        (40, 0, CheckOutcome.PASSED),
        (30, 0, CheckOutcome.PASSED),
        (25, 5, CheckOutcome.PASSED),
        (20, 10, CheckOutcome.FAILED),
        (20, 9, CheckOutcome.PASSED),
        (0, 40, CheckOutcome.FAILED),
    ],
)
def test_a13_compares_the_words_of_both_answers_at_the_exact_limits(
    raw_audit_full: RawAudit,
    with_website: Edit,
    shared_words: int,
    foreign_words: int,
    expected: CheckOutcome,
) -> None:
    visitor = _bot_text(WORDS_OF_THE_VISITOR)
    foreign = [f"fremdwort{number}" for number in range(foreign_words)]
    bot = _bot_text(WORDS_OF_THE_VISITOR[:shared_words] + foreign)
    assert raw_audit_full.website is not None
    fetches = [
        fetch.model_copy(update={"text": bot if fetch.agent == "googlebot" else visitor})
        for fetch in raw_audit_full.website.agent_fetches or []
    ]
    outcome, measured = run("a13", with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is expected
    assert measured["min_overlap"] == 0.5
    assert measured["compare_from_words"] == 30
    assert measured["agent"] == "googlebot"


@pytest.mark.parametrize(
    ("visitor_words", "bot_words", "expected"),
    [
        (100, 10, CheckOutcome.FAILED),
        (10, 100, CheckOutcome.FAILED),
        (99, 10, CheckOutcome.PASSED),
        (100, 11, CheckOutcome.PASSED),
    ],
)
def test_a13_a_version_without_text_is_found_at_the_exact_limits(
    raw_audit_full: RawAudit,
    with_website: Edit,
    visitor_words: int,
    bot_words: int,
    expected: CheckOutcome,
) -> None:
    visitor = _bot_text([f"besucher{number}" for number in range(visitor_words)])
    bot = _bot_text([f"bot{number}" for number in range(bot_words)])
    assert raw_audit_full.website is not None
    fetches = [
        fetch.model_copy(update={"text": bot if fetch.agent == "googlebot" else visitor})
        for fetch in raw_audit_full.website.agent_fetches or []
    ]
    outcome, measured = run("a13", with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is expected
    if expected is CheckOutcome.FAILED:
        assert measured["difference"] == "one_version_without_text"


def test_b22_compares_the_visitor_with_bingbot(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    different = _bot_text([f"fremdwort{number}" for number in range(40)])
    fetches = _fetches(raw_audit_full, "bingbot", text=different)
    raw = with_website(raw_audit_full, agent_fetches=fetches)
    assert run_b("b22", raw, {})[0] is CheckOutcome.FAILED
    assert run("a13", raw, {})[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", sorted(BOT_BY_CHECK))
def test_a_page_the_visitor_cannot_load_makes_the_comparison_not_measurable(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    fetches = _fetches(raw_audit_full, "visitor", status=503)
    outcome, measured = run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {"reason": "visitor_fetch_not_200", "visitor_status": 503}


@pytest.mark.parametrize("check_id", sorted(BOT_BY_CHECK))
def test_a_bot_request_that_failed_is_not_measurable(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    fetches = _fetches(raw_audit_full, BOT_BY_CHECK[check_id], status=None, error="ReadTimeout")
    outcome, measured = run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {"reason": "bot_fetch_failed", "bot_error": "ReadTimeout"}


@pytest.mark.parametrize("check_id", sorted(BOT_BY_CHECK))
def test_missing_bot_requests_are_not_measurable(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    assert run(check_id, with_website(raw_audit_full, agent_fetches=None), {})[1] == {
        "reason": "no_agent_fetches"
    }
    assert raw_audit_full.website is not None
    only_visitor = [
        fetch for fetch in raw_audit_full.website.agent_fetches or [] if fetch.agent == "visitor"
    ]
    assert run(check_id, with_website(raw_audit_full, agent_fetches=only_visitor), {})[1] == {
        "reason": "no_agent_fetches"
    }


def test_a_page_with_too_little_text_cannot_be_compared(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    short = " ".join(WORDS_OF_THE_VISITOR[:10])
    assert raw_audit_full.website is not None
    fetches = [
        fetch.model_copy(update={"text": short})
        for fetch in raw_audit_full.website.agent_fetches or []
    ]
    outcome, measured = run("a13", with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] == "no_comparable_text"


def test_a13_a_bot_error_status_is_not_a_content_comparison(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    fetches = _fetches(raw_audit_full, "googlebot", status=403)
    outcome, measured = run("a13", with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {"reason": "bot_fetch_not_200", "bot_status": 403}


@pytest.mark.parametrize("check_id", ["a16", "a18"])
@pytest.mark.parametrize("status", [401, 403])
def test_a_firewall_status_fails_the_bot(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str, status: int
) -> None:
    fetches = _fetches(raw_audit_full, BOT_BY_CHECK[check_id], status=status, text="")
    outcome, measured = run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.FAILED
    assert measured["reason_blocked"] == f"Status {status}"


@pytest.mark.parametrize("check_id", ["a16", "a18"])
@pytest.mark.parametrize("status", [404, 406, 451])
def test_another_error_status_is_unclear_and_not_measured(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str, status: int
) -> None:
    fetches = _fetches(raw_audit_full, BOT_BY_CHECK[check_id], status=status)
    outcome, measured = run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {
        "reason": "bot_status_unclear",
        "agent": BOT_BY_CHECK[check_id],
        "bot_status": status,
    }


@pytest.mark.parametrize("check_id", ["a16", "a18"])
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_rate_limit_or_server_error_is_not_a_firewall_finding(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str, status: int
) -> None:
    fetches = _fetches(raw_audit_full, BOT_BY_CHECK[check_id], status=status, text="")
    outcome, measured = run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {
        "reason": "bot_fetch_unavailable",
        "agent": BOT_BY_CHECK[check_id],
        "bot_status": status,
    }


@pytest.mark.parametrize("check_id", ["a16", "a18"])
def test_a_block_page_with_status_200_fails_the_bot(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    fetches = _fetches(
        raw_audit_full, BOT_BY_CHECK[check_id], text="Access denied. Checking your browser"
    )
    outcome, measured = run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})
    assert outcome is CheckOutcome.FAILED
    assert measured["reason_blocked"] == "block page"


@pytest.mark.parametrize("check_id", ["a16", "a18"])
def test_a_block_page_the_visitor_sees_too_is_no_firewall_finding(
    raw_audit_full: RawAudit, with_website: Edit, check_id: str
) -> None:
    text = "Forbidden area " + _bot_text(WORDS_OF_THE_VISITOR)
    assert raw_audit_full.website is not None
    fetches = [
        fetch.model_copy(update={"text": text})
        for fetch in raw_audit_full.website.agent_fetches or []
    ]
    assert run(check_id, with_website(raw_audit_full, agent_fetches=fetches), {})[0] is (
        CheckOutcome.PASSED
    )
