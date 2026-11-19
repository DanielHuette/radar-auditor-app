import pytest

from radarauditor.scoring.checks.robots import RobotsRules, directives_for

SITE = "https://www.beispiel.de"


def _allows(text: str | None, agent: str, path: str) -> bool:
    return RobotsRules(text).allows(agent, f"{SITE}{path}")


@pytest.mark.parametrize("text", [None, "", "# nur ein Kommentar", "Sitemap: https://x.de/s.xml"])
def test_without_rules_everything_is_allowed(text: str | None) -> None:
    assert _allows(text, "Googlebot", "/irgendwas/") is True


def test_the_wildcard_group_applies_to_every_agent() -> None:
    text = "User-agent: *\nDisallow: /intern/\n"
    assert _allows(text, "Googlebot", "/intern/seite") is False
    assert _allows(text, "Bingbot", "/intern/seite") is False
    assert _allows(text, "Googlebot", "/oeffentlich/") is True


def test_a_named_group_replaces_the_wildcard_group_for_that_agent() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n"
    assert _allows(text, "Googlebot", "/seite/") is True
    assert _allows(text, "Bingbot", "/seite/") is False


def test_agent_names_are_matched_without_regard_to_case() -> None:
    text = "user-AGENT: oai-searchbot\ndisallow: /\n"
    assert _allows(text, "OAI-SearchBot", "/") is False


def test_several_user_agent_lines_share_one_group() -> None:
    text = "User-agent: Googlebot\nUser-agent: Bingbot\nDisallow: /x/\n"
    assert _allows(text, "Googlebot", "/x/") is False
    assert _allows(text, "Bingbot", "/x/") is False
    assert _allows(text, "Claude-SearchBot", "/x/") is True


def test_the_longest_matching_rule_wins() -> None:
    text = "User-agent: *\nDisallow: /a/\nAllow: /a/b/\n"
    assert _allows(text, "Googlebot", "/a/b/c") is True
    assert _allows(text, "Googlebot", "/a/x") is False


def test_allow_wins_when_both_rules_are_equally_long() -> None:
    text = "User-agent: *\nDisallow: /x\nAllow: /x\n"
    assert _allows(text, "Googlebot", "/x") is True


def test_an_empty_disallow_allows_everything() -> None:
    assert _allows("User-agent: *\nDisallow:\n", "Googlebot", "/alles/") is True


OPEN_WILDCARD_ONE_BOT_BLOCKED = "User-agent: *\nDisallow:\n\nUser-agent: GPTBot\nDisallow: /\n"


@pytest.mark.parametrize(
    ("agent", "allowed"),
    [
        ("Googlebot", True),
        ("Bingbot", True),
        ("OAI-SearchBot", True),
        ("Claude-SearchBot", True),
        ("Claude-User", True),
        ("Google-Extended", True),
        ("GPTBot", False),
    ],
)
def test_an_open_wildcard_group_does_not_merge_with_the_next_group(
    agent: str, allowed: bool
) -> None:
    assert _allows(OPEN_WILDCARD_ONE_BOT_BLOCKED, agent, "/") is allowed


@pytest.mark.parametrize("blocked_bot", ["ClaudeBot", "CCBot"])
def test_a_sitemap_line_between_groups_does_not_close_or_merge_groups(blocked_bot: str) -> None:
    text = (
        "User-agent: *\nDisallow:\nSitemap: https://www.beispiel.de/sitemap.xml\n\n"
        f"User-agent: {blocked_bot}\nDisallow: /\n"
    )
    assert _allows(text, "Googlebot", "/") is True
    assert _allows(text, blocked_bot, "/") is False


def test_an_open_named_group_keeps_its_agent_out_of_a_blocking_wildcard_group() -> None:
    text = "User-agent: Googlebot\nDisallow:\n\nUser-agent: *\nDisallow: /\n"
    assert _allows(text, "Googlebot", "/") is True
    assert _allows(text, "Bingbot", "/") is False


def test_only_one_agent_allowed_and_everyone_else_blocked() -> None:
    text = "User-agent: Googlebot\nAllow: /\n\nUser-agent: *\nDisallow: /\n"
    assert _allows(text, "Googlebot", "/seite") is True
    assert _allows(text, "OAI-SearchBot", "/seite") is False


def test_several_user_agent_lines_above_an_empty_disallow_form_one_open_group() -> None:
    text = (
        "User-agent: Googlebot\nUser-agent: Bingbot\nDisallow:\n\nUser-agent: GPTBot\nDisallow: /\n"
    )
    assert _allows(text, "Googlebot", "/") is True
    assert _allows(text, "Bingbot", "/") is True
    assert _allows(text, "GPTBot", "/") is False


def test_groups_of_the_same_agent_are_combined() -> None:
    text = "User-agent: Googlebot\nDisallow: /a/\n\nUser-agent: Googlebot\nDisallow: /b/\n"
    assert _allows(text, "Googlebot", "/a/") is False
    assert _allows(text, "Googlebot", "/b/") is False
    assert _allows(text, "Googlebot", "/c/") is True


def test_user_agent_and_rule_keys_ignore_case_and_blank_and_comment_lines_between_groups() -> None:
    text = (
        "# Kopf\nUSER-AGENT: *\n# keine Sperre\nDISALLOW:\n\n\n"
        "user-agent: gptbot # Training\n\nDisallow: /\n"
    )
    assert _allows(text, "Googlebot", "/") is True
    assert _allows(text, "GPTBot", "/") is False


@pytest.mark.parametrize(
    ("path", "allowed"),
    [("/ordner/datei.pdf", False), ("/ordner/datei.pdf?x=1", True), ("/ordner/datei.html", True)],
)
def test_the_dollar_sign_anchors_the_end(path: str, allowed: bool) -> None:
    assert _allows("User-agent: *\nDisallow: /*.pdf$\n", "Googlebot", path) is allowed


def test_a_star_matches_any_characters_and_the_query_counts() -> None:
    text = "User-agent: *\nDisallow: /*?sitzung=\n"
    assert _allows(text, "Googlebot", "/seite?sitzung=1") is False
    assert _allows(text, "Googlebot", "/seite?anderes=1") is True


def test_a_rule_is_a_prefix() -> None:
    text = "User-agent: *\nDisallow: /ratgeber\n"
    assert _allows(text, "Googlebot", "/ratgeber/artikel/") is False
    assert _allows(text, "Googlebot", "/ratgeberin/") is False
    assert _allows(text, "Googlebot", "/andere/") is True


def test_comments_and_blank_lines_are_ignored() -> None:
    text = "# Kopf\nUser-agent: * # alle\n\nDisallow: /geheim/ # nicht lesen\n"
    assert _allows(text, "Googlebot", "/geheim/") is False


def test_rules_before_any_user_agent_line_are_ignored() -> None:
    assert _allows("Disallow: /\nUser-agent: *\nAllow: /\n", "Googlebot", "/") is True


def test_the_root_path_of_an_address_without_a_slash_is_the_root() -> None:
    assert RobotsRules("User-agent: *\nDisallow: /\n").allows("Googlebot", SITE) is False


def test_a_byte_order_mark_at_the_start_is_ignored() -> None:
    text = "\ufeffUser-agent: *\nDisallow: /\n"
    assert _allows(text, "Googlebot", "/seite/") is False


def test_a_byte_order_mark_does_not_hide_the_first_group_from_a_named_agent() -> None:
    text = "\ufeffUser-agent: OAI-SearchBot\nDisallow: /\n"
    assert _allows(text, "OAI-SearchBot", "/") is False


@pytest.mark.parametrize(
    "line",
    [
        "Googlebot/2.1",
        "googlebot/1.2",
        "Googlebot*",
        "GOOGLEBOT/2.1 (+http://www.google.com/bot.html)",
    ],
)
def test_a_version_after_the_product_token_is_ignored(line: str) -> None:
    assert _allows(f"User-agent: {line}\nDisallow: /\n", "Googlebot", "/") is False


def test_a_longer_product_token_is_another_agent() -> None:
    assert _allows("User-agent: Googlebot-Image\nDisallow: /\n", "Googlebot", "/") is True


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ("/über-uns", "/%C3%BCber-uns"),
        ("/%C3%BCber-uns", "/über-uns"),
        ("/%c3%bcber-uns", "/%C3%BCber-uns"),
        ("/%61bout", "/about"),
        ("/about", "/%61bout"),
    ],
)
def test_rules_and_addresses_are_compared_in_their_percent_encoded_form(
    rule: str, path: str
) -> None:
    assert _allows(f"User-agent: *\nDisallow: {rule}\n", "Googlebot", path) is False


def test_an_encoded_slash_is_not_a_slash() -> None:
    assert _allows("User-agent: *\nDisallow: /a/b\n", "Googlebot", "/a%2Fb") is True


@pytest.mark.parametrize(
    ("value", "agent", "expected"),
    [
        (None, "Googlebot", set()),
        ("", "Googlebot", set()),
        ("noindex, nofollow", "Googlebot", {"noindex", "nofollow"}),
        ("noindex, nofollow", "Bingbot", {"noindex", "nofollow"}),
        ("googlebot: noindex", "Googlebot", {"noindex"}),
        ("googlebot: noindex", "Bingbot", set()),
        ("bingbot: noarchive, nosnippet", "Bingbot", {"noarchive", "nosnippet"}),
        ("bingbot: noarchive, nosnippet", "Googlebot", set()),
        ("googlebot: noindex\nnofollow", "Bingbot", {"nofollow"}),
        ("googlebot: noindex\nnofollow", "Googlebot", {"noindex", "nofollow"}),
        ("max-snippet: 0", "Googlebot", {"max-snippet:0"}),
        ("NOINDEX", "Googlebot", {"noindex"}),
    ],
)
def test_directives_for_an_agent(value: str | None, agent: str, expected: set[str]) -> None:
    assert directives_for(value, agent) == expected
