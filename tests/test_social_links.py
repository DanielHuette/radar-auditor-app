import pytest
from conftest import SITE, build_website
from pydantic import HttpUrl

from radarauditor.collectors import social_links
from radarauditor.collectors.snapshot import SocialPlatform, WebsiteSnapshot
from radarauditor.collectors.social_links import AccountRef


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (
            "https://www.instagram.com/KatjaKarsch/",
            AccountRef("instagram", "katjakarsch", "https://www.instagram.com/katjakarsch/"),
        ),
        (
            "http://instagram.com/katja.karsch?hl=de",
            AccountRef("instagram", "katja.karsch", "https://www.instagram.com/katja.karsch/"),
        ),
        ("https://www.instagram.com/p/DfKteZpMeMn/", None),
        ("https://www.instagram.com/explore/tags/kunst/", None),
        ("https://www.instagram.com/", None),
        (
            "https://www.instagram.com/" + "a" * 30,
            AccountRef("instagram", "a" * 30, "https://www.instagram.com/" + "a" * 30 + "/"),
        ),
        ("https://www.instagram.com/" + "a" * 31, None),
        (
            "https://www.facebook.com/dachdeckereimuster/",
            AccountRef(
                "facebook", "dachdeckereimuster", "https://www.facebook.com/dachdeckereimuster"
            ),
        ),
        (
            "https://de-de.facebook.com/dachdeckereimuster",
            AccountRef(
                "facebook", "dachdeckereimuster", "https://www.facebook.com/dachdeckereimuster"
            ),
        ),
        (
            "https://m.facebook.com/Dachdecker.Muster",
            AccountRef(
                "facebook", "dachdecker.muster", "https://www.facebook.com/Dachdecker.Muster"
            ),
        ),
        (
            "https://www.facebook.com/profile.php?id=100044561550831",
            AccountRef(
                "facebook",
                "100044561550831",
                "https://www.facebook.com/profile.php?id=100044561550831",
            ),
        ),
        ("https://www.facebook.com/profile.php?id=abc", None),
        ("https://www.facebook.com/profile.php", None),
        (
            "https://www.facebook.com/pages/Dachdecker-Muster/123456789",
            AccountRef(
                "facebook",
                "123456789",
                "https://www.facebook.com/pages/Dachdecker-Muster/123456789",
            ),
        ),
        ("https://www.facebook.com/pages/Dachdecker-Muster", None),
        (
            "https://www.facebook.com/p/Dachdecker-Muster-100054301309039/",
            AccountRef(
                "facebook",
                "dachdecker-muster-100054301309039",
                "https://www.facebook.com/p/Dachdecker-Muster-100054301309039",
            ),
        ),
        ("https://www.facebook.com/p/", None),
        (
            "https://www.facebook.com/people/Dachdecker-Muster/100054301309039/",
            AccountRef(
                "facebook",
                "100054301309039",
                "https://www.facebook.com/people/Dachdecker-Muster/100054301309039",
            ),
        ),
        ("https://www.facebook.com/people/Dachdecker-Muster", None),
        ("https://www.facebook.com/sharer/sharer.php?u=https://x.de", None),
        ("https://www.facebook.com/plugins/like.php", None),
        ("https://www.facebook.com/", None),
        (
            "https://www.linkedin.com/company/dachdeckerei-muster/",
            AccountRef(
                "linkedin_company",
                "dachdeckerei-muster",
                "https://www.linkedin.com/company/dachdeckerei-muster",
            ),
        ),
        (
            "https://de.linkedin.com/in/Katja-Karsch-009aa23b0?trk=x",
            AccountRef(
                "linkedin_person",
                "katja-karsch-009aa23b0",
                "https://www.linkedin.com/in/Katja-Karsch-009aa23b0",
            ),
        ),
        ("https://www.linkedin.com/company/", None),
        ("https://www.linkedin.com/in", None),
        ("https://www.linkedin.com/showcase/produkt/", None),
        ("https://www.linkedin.com/feed/", None),
        ("https://x.com/Apify", AccountRef("x", "apify", "https://x.com/apify")),
        ("https://twitter.com/apify/status/1", AccountRef("x", "apify", "https://x.com/apify")),
        ("https://x.com/intent/tweet?text=hi", None),
        ("https://x.com/" + "a" * 15, AccountRef("x", "a" * 15, "https://x.com/" + "a" * 15)),
        ("https://x.com/" + "a" * 16, None),
        ("https://x.com/", None),
        ("https://www.example.de/instagram.com/katjakarsch", None),
        ("https://notinstagram.com/katjakarsch", None),
        ("mailto:info@example.de", None),
        ("not a link", None),
    ],
)
def test_account_from_link(link: str, expected: AccountRef | None) -> None:
    assert social_links.account_from_link(link) == expected


def _site(
    start_links: list[str],
    legal_links: list[str] | None = None,
    other_links: list[str] | None = None,
) -> WebsiteSnapshot:
    site = build_website()
    legal_url = HttpUrl(f"{SITE}/impressum/")
    pages = []
    for page in site.pages:
        links: list[str] = []
        if str(page.url) == f"{SITE}/":
            links = start_links
        elif str(page.url) == str(legal_url):
            links = legal_links or []
        elif str(page.url) == f"{SITE}/kontakt/":
            links = other_links or []
        pages.append(page.model_copy(update={"external_links": [HttpUrl(link) for link in links]}))
    return site.model_copy(update={"pages": pages, "legal_notice_url": legal_url})


def test_linked_accounts_takes_the_most_linked_account_per_platform() -> None:
    site = _site(
        ["https://www.instagram.com/partner/", "https://www.instagram.com/eigen/"],
        other_links=["https://www.instagram.com/eigen/"],
    )

    linked = social_links.linked_accounts(site)

    assert linked["instagram"][0].handle == "eigen"


def test_linked_accounts_keeps_page_order_on_equal_counts() -> None:
    site = _site(["https://www.instagram.com/erster/", "https://www.instagram.com/zweiter/"])

    assert social_links.linked_accounts(site)["instagram"][0].handle == "erster"


def test_linked_accounts_names_the_legal_notice_as_source() -> None:
    site = _site(
        ["https://www.instagram.com/eigen/"],
        legal_links=["https://www.linkedin.com/in/katja-karsch-009aa23b0"],
    )

    linked = social_links.linked_accounts(site)

    assert linked["instagram"][1] == "website_link"
    assert linked["linkedin_person"][1] == "legal_notice_link"


def test_linked_accounts_prefers_legal_notice_source_when_both_link() -> None:
    link = "https://www.instagram.com/eigen/"

    linked = social_links.linked_accounts(_site([link], legal_links=[link]))

    assert linked["instagram"][1] == "legal_notice_link"


def test_linked_accounts_ignores_other_sites_and_finds_all_platforms() -> None:
    site = _site(
        [
            "https://www.hwk-muenster.de/",
            "https://www.facebook.com/dachdeckereimuster",
            "https://www.linkedin.com/company/dachdeckerei-muster",
            "https://x.com/dachdecker",
        ]
    )

    linked = social_links.linked_accounts(site)

    assert sorted(linked) == ["facebook", "linkedin_company", "x"]


def test_linked_accounts_leave_out_the_accounts_of_the_site_builder() -> None:
    site = _site(
        [
            "https://www.facebook.com/wix",
            "https://www.instagram.com/wix",
            "https://www.linkedin.com/company/wix-com",
            "https://x.com/wix",
            "https://www.instagram.com/wixel/",
        ]
    )

    linked = social_links.linked_accounts(site)

    assert {platform: ref.handle for platform, (ref, _) in linked.items()} == {"instagram": "wixel"}


def test_linked_accounts_is_empty_without_links() -> None:
    assert social_links.linked_accounts(_site([])) == {}


def test_name_variants_keep_order_and_drop_duplicates_ignoring_case() -> None:
    variants = social_links.name_variants(
        [None, "Katja  Karsch", "katja karsch", "", "Atelier Karsch"], "www.katjakarsch.de"
    )

    assert variants == ["Katja Karsch", "Atelier Karsch", "katjakarsch"]


def test_name_variants_are_limited_to_three() -> None:
    variants = social_links.name_variants(
        ["A Name", "B Name", "C Name", "D Name"], "katjakarsch.de"
    )

    assert variants == ["A Name", "B Name", "C Name"]


@pytest.mark.parametrize(("host", "expected"), [("ab.de", []), ("abc.de", ["abc"])])
def test_name_variants_use_a_domain_stem_of_at_least_three_characters(
    host: str, expected: list[str]
) -> None:
    assert social_links.name_variants([], host) == expected


def test_name_variants_do_not_repeat_a_name_equal_to_the_domain_stem() -> None:
    assert social_links.name_variants(["KatjaKarsch"], "katjakarsch.de") == ["KatjaKarsch"]


@pytest.mark.parametrize(
    ("platform", "scope"),
    [
        ("instagram", "instagram.com"),
        ("facebook", "facebook.com"),
        ("linkedin_company", "linkedin.com/company"),
        ("linkedin_person", "linkedin.com/in"),
        ("x", "x.com"),
    ],
)
def test_search_phrase_restricts_the_search_to_the_platform(
    platform: SocialPlatform, scope: str
) -> None:
    assert social_links.search_phrase(platform, "Katja Karsch") == f'site:{scope} "Katja Karsch"'


def test_name_variants_drop_quotation_marks_that_would_end_the_search_phrase() -> None:
    assert social_links.name_variants(['Atelier "Licht" Karsch'], "ab.de") == [
        "Atelier Licht Karsch"
    ]


@pytest.mark.parametrize(
    ("platform", "link", "handles"),
    [
        ("instagram", "https://www.instagram.com/katjakarsch/?hl=de", ["katjakarsch"]),
        ("instagram", "https://www.instagram.com/katjakarsch/reels/", []),
        ("facebook", "https://www.facebook.com/muster/", ["muster"]),
        ("facebook", "https://www.facebook.com/muster/posts/123/", []),
        ("facebook", "https://www.facebook.com/profile.php?id=42", ["42"]),
        ("facebook", "https://www.facebook.com/p/Muster-42/", ["muster-42"]),
        ("facebook", "https://www.facebook.com/p/Muster-42/photos/", []),
        ("linkedin_company", "https://de.linkedin.com/company/muster", ["muster"]),
        ("linkedin_company", "https://www.linkedin.com/company/muster/jobs", []),
        ("linkedin_person", "https://www.linkedin.com/in/max-muster", ["max-muster"]),
        ("linkedin_person", "https://www.linkedin.com/in/max-muster/recent-activity", []),
        ("x", "https://x.com/muster", ["muster"]),
        ("x", "https://x.com/muster/status/1", []),
    ],
)
def test_accounts_from_results_take_only_links_to_the_profile_itself(
    platform: SocialPlatform, link: str, handles: list[str]
) -> None:
    found = social_links.accounts_from_results(platform, [link])

    assert [ref.handle for ref in found] == handles


@pytest.mark.parametrize(
    ("variants", "name", "handle", "expected"),
    [
        (["Katja Karsch"], "Katja Karsch | Art", "kk", True),
        (["Katja Karsch"], "KARSCH, Katja", "kk", True),
        (["Katja Karsch"], "Katja Müller", "kk", False),
        (["Katja Karsch"], None, "katja.karsch_art", True),
        (["Katja Karsch"], None, "katja", False),
        (["katjakarsch"], "Katja Karsch", "kk", True),
        (["Atelier Nord", "Katja Karsch"], "Katja Karsch", "kk", True),
        (["&"], "Katja Karsch", "kk", False),
        ([], "Katja Karsch", "katjakarsch", False),
    ],
)
def test_carries_name_compares_words_and_the_name_written_together(
    variants: list[str], name: str | None, handle: str, expected: bool
) -> None:
    assert social_links.carries_name(variants, name, handle) is expected


def test_accounts_from_results_keeps_one_account_per_handle_of_the_platform() -> None:
    links = [
        "https://www.instagram.com/p/abc/",
        "https://www.instagram.com/katjakarsch/",
        "https://www.facebook.com/katjakarsch",
        "https://www.instagram.com/KatjaKarsch/?hl=de",
        "https://www.instagram.com/other/",
    ]

    found = social_links.accounts_from_results("instagram", links)

    assert [ref.handle for ref in found] == ["katjakarsch", "other"]


def test_platforms_run_in_report_order() -> None:
    assert social_links.PLATFORMS == (
        "instagram",
        "facebook",
        "linkedin_company",
        "linkedin_person",
        "x",
    )
