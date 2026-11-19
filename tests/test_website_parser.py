import httpx
import pytest
import respx
from pydantic import HttpUrl

from radarauditor.collectors import website
from radarauditor.collectors.snapshot import PageSnapshot
from radarauditor.errors import CollectionError, TransientCollectionError

BASE = "https://www.dachdecker-hollenbeck.de"

PAGE_HTML = """<!doctype html>
<html lang="de">
<head>
<title>  Dachdeckerei Hollenbeck
 | Dachdecker in Münster </title>
<meta name="description" content="Steildach,   Flachdach und Notdienst in Münster.">
<link rel="canonical" href="/">
<meta property="og:title" content="Dachdeckerei Hollenbeck">
<meta property="og:type" content="website">
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "RoofingContractor",
 "name": "Dachdeckerei Hollenbeck GmbH"}</script>
<script type="application/ld+json">[{"@type": "WebSite"}, {"@type": "BreadcrumbList"}]</script>
<script type="application/ld+json">{"@type": "Organization", "name": "broken",}</script>
<style>body { color: red; }</style>
</head>
<body>
<h1>Ihr   Dachdecker in Münster</h1>
<h2>Leistungen</h2>
<h3></h3>
<h3>Flachdach</h3>
<p>Wir decken Dächer seit 1987.</p>
<script>var tracking = "do not count these words";</script>
<img src="/bilder/team.jpg" alt="Unser Team">
<img src="/bilder/dach.jpg">
<img src="https://cdn.example.org/logo.png" alt="">
<img src="data:image/png;base64,AAAA" alt="Pixel">
<a href="/leistungen#flachdach">Leistungen</a>
<a href="/leistungen">Leistungen nochmal</a>
<a href="https://dachdecker-hollenbeck.de/kontakt">Kontakt</a>
<a href="https://www.facebook.com/hollenbeck">Facebook</a>
<a href="#top">Nach oben</a>
<a href="tel:+49251123456">Anrufen</a>
<a href="tel:+49251123456">Anrufen</a>
<a href="mailto:info@dachdecker-hollenbeck.de?subject=Anfrage">E-Mail</a>
<a href="javascript:void(0)">Menü</a>
</body>
</html>
"""


def _page(url: str, title: str | None = None, links: list[str] | None = None) -> PageSnapshot:
    return PageSnapshot(
        url=HttpUrl(url),
        status_code=200,
        title=title,
        meta_description=None,
        canonical=None,
        lang="de",
        headings=[],
        word_count=0,
        json_ld=[],
        open_graph={},
        internal_links=[HttpUrl(link) for link in links or []],
        images_without_alt=0,
        oversized_images=0,
        tel_links=[],
        mailto_links=[],
        text_excerpt="",
    )


def test_parse_page_reads_head_values() -> None:
    page = website.parse_page(HttpUrl(f"{BASE}/start"), 200, PAGE_HTML)

    assert page.status_code == 200
    assert page.title == "Dachdeckerei Hollenbeck | Dachdecker in Münster"
    assert page.meta_description == "Steildach, Flachdach und Notdienst in Münster."
    assert str(page.canonical) == f"{BASE}/"
    assert page.lang == "de"
    assert page.open_graph == {"og:title": "Dachdeckerei Hollenbeck", "og:type": "website"}


def test_parse_page_reads_headings_in_order_and_skips_empty_ones() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, PAGE_HTML)

    assert page.headings == [(1, "Ihr Dachdecker in Münster"), (2, "Leistungen"), (3, "Flachdach")]


def test_parse_page_parses_json_ld_and_skips_invalid_blocks() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, PAGE_HTML)

    assert [block["@type"] for block in page.json_ld] == [
        "RoofingContractor",
        "WebSite",
        "BreadcrumbList",
    ]


def test_parse_page_collects_internal_links_absolute_and_deduplicated() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, PAGE_HTML)

    assert [str(link) for link in page.internal_links] == [
        f"{BASE}/leistungen",
        "https://dachdecker-hollenbeck.de/kontakt",
    ]


def test_parse_page_counts_images_and_contact_links() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, PAGE_HTML)

    assert page.images_without_alt == 2
    assert page.tel_links == ["+49251123456"]
    assert page.mailto_links == ["info@dachdecker-hollenbeck.de"]


def test_parse_page_reads_visible_text_only() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, PAGE_HTML)

    assert page.text_excerpt.startswith("Ihr Dachdecker in Münster Leistungen Flachdach")
    assert "tracking" not in page.text_excerpt
    assert "color" not in page.text_excerpt
    assert page.word_count == len(page.text_excerpt.split())


def test_parse_page_truncates_excerpt() -> None:
    html = "<html><body><p>" + "wort " * 400 + "</p></body></html>"

    page = website.parse_page(HttpUrl(BASE), 200, html)

    assert len(page.text_excerpt) == website.TEXT_EXCERPT_CHARS
    assert page.word_count == 400


def test_parse_page_handles_empty_document() -> None:
    page = website.parse_page(HttpUrl(BASE), 404, "")

    assert page.status_code == 404
    assert page.title is None
    assert page.meta_description is None
    assert page.canonical is None
    assert page.headings == []
    assert page.word_count == 0
    assert page.has_viewport_meta is False
    assert page.external_links == []
    assert page.third_party_hosts == []


EMBED_HTML = """<!doctype html>
<html lang="de">
<head>
<meta name="Viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto">
<link rel="stylesheet" href="/css/site.css">
<link rel="preconnect" href="https://preconnect.example.net">
<script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script>
<script src="https://cdn.dachdecker-hollenbeck.de/app.js"></script>
<style>
@import url("https://fonts.bunny.net/css?family=lato");
@font-face { font-family: X; src: url(https://fonts.gstatic.com/s/x.woff2) format("woff2"); }
.hero { background: url(https://images.example.org/hero.jpg); }
</style>
</head>
<body>
<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>
<img src="https://cdn.example.org/logo.png" alt="Logo">
<img data-src="https://lazy.example.org/bild.jpg" alt="Bild">
<img src="/bilder/eigen.jpg" alt="Eigen">
<div style="background: url(https://inline.example.org/a.png)"></div>
<a href="https://www.hwk-muenster.de/">Handwerkskammer</a>
<a href="https://www.hwk-muenster.de/#mitglieder">Handwerkskammer nochmal</a>
<a href="https://www.facebook.com/hollenbeck">Facebook</a>
<a href="https://shop.dachdecker-hollenbeck.de/">Shop</a>
<a href="/kontakt">Kontakt</a>
</body>
</html>
"""


def test_parse_page_detects_viewport_meta() -> None:
    assert website.parse_page(HttpUrl(BASE), 200, EMBED_HTML).has_viewport_meta is True
    assert website.parse_page(HttpUrl(BASE), 200, PAGE_HTML).has_viewport_meta is False


def test_parse_page_collects_external_links_absolute_and_deduplicated() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, EMBED_HTML)

    assert [str(link) for link in page.external_links] == [
        "https://www.hwk-muenster.de/",
        "https://www.facebook.com/hollenbeck",
        "https://shop.dachdecker-hollenbeck.de/",
    ]
    assert [str(link) for link in page.internal_links] == [f"{BASE}/kontakt"]


def test_parse_page_collects_third_party_hosts_without_own_hosts() -> None:
    page = website.parse_page(HttpUrl(BASE), 200, EMBED_HTML)

    assert page.third_party_hosts == [
        "www.googletagmanager.com",
        "www.google.com",
        "cdn.example.org",
        "lazy.example.org",
        "fonts.googleapis.com",
        "fonts.bunny.net",
        "fonts.gstatic.com",
    ]


def test_parse_page_keeps_long_excerpt_for_privacy_page() -> None:
    html = "<html><body><p>" + "datenschutz " * 1000 + "</p></body></html>"

    privacy = website.parse_page(HttpUrl(f"{BASE}/datenschutz/"), 200, html)
    other = website.parse_page(HttpUrl(f"{BASE}/leistungen/"), 200, html)

    assert len(privacy.text_excerpt) > website.TEXT_EXCERPT_CHARS
    assert len(privacy.text_excerpt) <= website.PRIVACY_EXCERPT_CHARS
    assert len(other.text_excerpt) == website.TEXT_EXCERPT_CHARS


def test_find_legal_pages_by_crawled_url_and_title() -> None:
    pages = [
        _page(f"{BASE}/"),
        _page(f"{BASE}/impressum"),
        _page(f"{BASE}/rechtliches", title="Datenschutzerklärung"),
    ]

    legal, privacy = website.find_legal_pages(pages)

    assert str(legal) == f"{BASE}/impressum"
    assert str(privacy) == f"{BASE}/rechtliches"


def test_find_legal_pages_falls_back_to_links() -> None:
    pages = [_page(f"{BASE}/", links=[f"{BASE}/legal-notice", f"{BASE}/privacy-policy"])]

    legal, privacy = website.find_legal_pages(pages)

    assert str(legal) == f"{BASE}/legal-notice"
    assert str(privacy) == f"{BASE}/privacy-policy"


def test_find_legal_pages_returns_none_when_missing() -> None:
    assert website.find_legal_pages([_page(f"{BASE}/")]) == (None, None)


def test_extract_footer_nap_reads_multi_line_footer() -> None:
    html = """<html><body><main>Willkommen</main><footer>
    <p>Dachdeckerei Hollenbeck GmbH</p><p>Hiltruper Straße 210</p><p>48165 Münster-Hiltrup</p>
    <p>Fax: 0251 / 123 45 68</p><p>Tel.: 0251 / 123 45 67</p></footer></body></html>"""

    assert website.extract_footer_nap(html) == {
        "name": "Dachdeckerei Hollenbeck GmbH",
        "street": "Hiltruper Straße 210",
        "postal_code": "48165",
        "city": "Münster-Hiltrup",
        "phone": "0251 / 123 45 67",
    }


def test_extract_footer_nap_reads_single_line_with_international_phone() -> None:
    html = """<html><body><div class="site-footer">Bedachungen Overmann · Roxeler Str. 12a,
    48161 Münster · +49 251 9876543</div></body></html>"""

    nap = website.extract_footer_nap(html)

    assert nap["street"] == "Roxeler Str. 12a"
    assert nap["postal_code"] == "48161"
    assert nap["city"] == "Münster"
    assert nap["phone"] == "+49 251 9876543"


def test_extract_footer_nap_reads_contact_block_and_skips_label() -> None:
    html = """<html><body><div id="kontakt"><h3>Kontakt</h3><address>Malerbetrieb Sander<br>
    Wolbecker Straße 5<br>48155 Münster</address><p>Telefon 0251 1111 22</p></div>
    </body></html>"""

    assert website.extract_footer_nap(html) == {
        "name": "Malerbetrieb Sander",
        "street": "Wolbecker Straße 5",
        "postal_code": "48155",
        "city": "Münster",
        "phone": "0251 1111 22",
    }


def test_extract_footer_nap_falls_back_to_body_and_finds_phone_anywhere() -> None:
    html = """<html><body><footer><p>Telefon: 02501 44 55 66</p></footer>
    <h1>Impressum</h1><p>Zimmerei Lüttke</p><p>Am Dorfbach 3 · 48167 Münster</p></body></html>"""

    nap = website.extract_footer_nap(html)

    assert nap["street"] == "Am Dorfbach 3"
    assert nap["postal_code"] == "48167"
    assert nap["phone"] == "02501 44 55 66"


def test_extract_footer_nap_without_address_returns_empty() -> None:
    assert website.extract_footer_nap("<html><body><footer>© 2026</footer></body></html>") == {}


def test_extract_opening_hours_matches_weekday_with_time() -> None:
    lines = ["Öffnungszeiten", "Mo.–Fr. 7:30 – 17:00 Uhr", "Sa 9.00-12.00", "So geht es weiter"]

    assert website.extract_opening_hours(lines) == "Mo.–Fr. 7:30 – 17:00 Uhr\nSa 9.00-12.00"
    assert website.extract_opening_hours(["Montags geschlossen"]) is None


@respx.mock
def test_check_single_host_redirects_true_when_all_variants_end_on_one_host() -> None:
    canonical = "https://www.example.de/"
    respx.get("http://example.de/").mock(
        return_value=httpx.Response(301, headers={"Location": canonical})
    )
    respx.get("http://www.example.de/").mock(
        return_value=httpx.Response(301, headers={"Location": canonical})
    )
    respx.get("https://example.de/").mock(
        return_value=httpx.Response(301, headers={"Location": canonical})
    )
    respx.get(canonical).mock(return_value=httpx.Response(200, text="ok"))

    assert website.check_single_host_redirects(HttpUrl("https://example.de")) is True


@respx.mock
def test_check_single_host_redirects_false_when_a_variant_stays_on_its_host() -> None:
    respx.get("http://example.de/").mock(return_value=httpx.Response(200, text="duplicate"))
    respx.get("http://www.example.de/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://www.example.de/"})
    )
    respx.get("https://example.de/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://www.example.de/"})
    )
    respx.get("https://www.example.de/").mock(return_value=httpx.Response(200, text="ok"))

    assert website.check_single_host_redirects(HttpUrl("https://www.example.de/")) is False


@respx.mock
def test_check_single_host_redirects_skips_unreachable_variants() -> None:
    respx.get("http://example.de/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://example.de/"})
    )
    respx.get("https://example.de/").mock(return_value=httpx.Response(200, text="ok"))
    respx.get("http://www.example.de/").mock(side_effect=httpx.ConnectError("no dns"))
    respx.get("https://www.example.de/").mock(side_effect=httpx.ConnectError("no dns"))

    assert website.check_single_host_redirects(HttpUrl("https://example.de/")) is True


@respx.mock
def test_check_single_host_redirects_false_when_nothing_answers() -> None:
    respx.get(url__regex=r".*example\.de/$").mock(side_effect=httpx.ConnectError("down"))

    assert website.check_single_host_redirects(HttpUrl("https://example.de/")) is False


ROBOTS = """User-agent: *
Disallow:

User-agent: RepoCity-RadarAuditor
Disallow: /intern/

Sitemap: https://www.example.de/sitemap_index.xml
"""

START_HTML = """<html lang="de"><head><title>Start</title></head><body>
<h1>Willkommen</h1><p>Mo-Fr 8:00 - 17:00 Uhr</p>
<img src="/gross.jpg" alt="gross"><img src="/klein.jpg" alt="klein">
<a href="/leistungen">Leistungen</a><a href="/intern/geheim">Intern</a>
<a href="/impressum">Impressum</a><a href="/preisliste.pdf">Preise</a>
<a href="https://andere-firma.de/">Partner</a>
<footer>Beispiel Bedachungen<br>Hafenweg 4<br>48155 Münster</footer>
</body></html>"""

SERVICES_HTML = """<html><head><title>Leistungen</title>
<script type="application/ld+json">{"@type": "Service",}</script></head><body>
<h1>Leistungen</h1><img src="/gross.jpg" alt="gross"><a href="/">Start</a>
<a href="/kontakt">Kontakt</a></body></html>"""

IMPRINT_HTML = """<html><head><title>Impressum</title></head><body><h1>Impressum</h1>
<p>Beispiel Bedachungen GmbH</p><p>Telefon: 0251 765432</p></body></html>"""


def _mock_site(router: respx.MockRouter) -> None:
    router.get("https://www.example.de/").mock(return_value=httpx.Response(200, html=START_HTML))
    router.get("https://www.example.de/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS)
    )
    router.get("https://www.example.de/sitemap.xml").mock(return_value=httpx.Response(404))
    router.get("https://www.example.de/sitemap_index.xml").mock(return_value=httpx.Response(404))
    router.get("https://www.example.de/leistungen").mock(
        return_value=httpx.Response(200, html=SERVICES_HTML)
    )
    router.get("https://www.example.de/impressum").mock(
        return_value=httpx.Response(200, html=IMPRINT_HTML)
    )
    router.get("https://www.example.de/kontakt").mock(side_effect=httpx.ReadTimeout("slow"))
    for moved in (
        "http://example.de/",
        "http://www.example.de/",
        "https://example.de/",
    ):
        router.get(moved).mock(
            return_value=httpx.Response(301, headers={"Location": "https://www.example.de/"})
        )
    router.get("https://www.example.de/leistungen/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://www.example.de/leistungen"})
    )


@pytest.fixture
def quiet_crawler(monkeypatch: pytest.MonkeyPatch) -> list[HttpUrl]:
    checked: list[HttpUrl] = []

    def fake_redirect_check(start_url: HttpUrl) -> bool:
        checked.append(start_url)
        return True

    monkeypatch.setattr(website, "POLITE_DELAY_SECONDS", 0)
    monkeypatch.setattr(website, "check_single_host_redirects", fake_redirect_check)
    return checked


async def test_crawl_website_collects_site_facts(quiet_crawler: list[HttpUrl]) -> None:
    with respx.mock(assert_all_called=True) as router:
        _mock_site(router)
        snapshot = await website.crawl_website(HttpUrl("https://www.example.de/"))

    assert [str(page.url) for page in snapshot.pages] == [
        "https://www.example.de/",
        "https://www.example.de/leistungen",
        "https://www.example.de/impressum",
    ]
    assert snapshot.uses_https is True
    assert snapshot.redirects_to_single_host is True
    assert quiet_crawler == [HttpUrl("https://www.example.de/")]
    assert snapshot.robots_txt == ROBOTS
    assert [str(url) for url in snapshot.sitemap_urls] == [
        "https://www.example.de/sitemap_index.xml"
    ]
    assert str(snapshot.legal_notice_url) == "https://www.example.de/impressum"
    assert snapshot.privacy_url is None
    assert snapshot.opening_hours_text == "Mo-Fr 8:00 - 17:00 Uhr"
    assert snapshot.footer_nap == {
        "name": "Beispiel Bedachungen",
        "street": "Hafenweg 4",
        "postal_code": "48155",
        "city": "Münster",
        "phone": "0251 765432",
    }


async def test_crawl_website_reports_errors_and_obeys_robots(
    quiet_crawler: list[HttpUrl],
) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_site(router)
        snapshot = await website.crawl_website(HttpUrl("https://www.example.de/"))
        head_calls = [call for call in router.calls if call.request.method == "HEAD"]

    assert [page.images_without_alt for page in snapshot.pages] == [0, 0, 0]
    assert head_calls == []
    assert any("invalid JSON-LD" in error for error in snapshot.crawl_errors)
    assert "https://www.example.de/kontakt: ReadTimeout" in snapshot.crawl_errors
    assert not any("intern" in str(page.url) for page in snapshot.pages)


async def test_crawl_website_respects_page_budget(quiet_crawler: list[HttpUrl]) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_site(router)
        snapshot = await website.crawl_website(HttpUrl("https://www.example.de/"), max_pages=2)

    assert len(snapshot.pages) == 2


@pytest.mark.usefixtures("fast_retries")
async def test_crawl_website_raises_when_start_page_missing(
    quiet_crawler: list[HttpUrl],
) -> None:
    with respx.mock:
        respx.get("https://www.example.de/").mock(return_value=httpx.Response(404))
        with pytest.raises(CollectionError):
            await website.crawl_website(HttpUrl("https://www.example.de/"))


@pytest.mark.usefixtures("fast_retries")
async def test_crawl_website_retries_and_raises_when_start_page_unreachable(
    quiet_crawler: list[HttpUrl],
) -> None:
    with respx.mock:
        route = respx.get("https://www.example.de/").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(TransientCollectionError):
            await website.crawl_website(HttpUrl("https://www.example.de/"))

    assert route.call_count == 3


ROBOTS_URL = "https://www.example.de/robots.txt"


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
async def test_a_byte_order_mark_does_not_reach_the_stored_robots_txt_or_the_crawler(
    prefix: bytes,
) -> None:
    body = prefix + b"User-agent: *\nDisallow: /intern/\n"
    with respx.mock() as router:
        router.get(ROBOTS_URL).mock(return_value=httpx.Response(200, content=body))
        async with httpx.AsyncClient() as client:
            text, status = await website._fetch_robots(client, ROBOTS_URL)

    assert status == 200
    assert text == "User-agent: *\nDisallow: /intern/\n"
    rules = website._robot_rules(text)
    assert rules.can_fetch("Googlebot", "https://www.example.de/intern/x") is False


@pytest.mark.parametrize("status", [404, 503])
async def test_a_robots_txt_that_is_not_served_has_no_text_but_keeps_its_status(
    status: int,
) -> None:
    with respx.mock() as router:
        router.get(ROBOTS_URL).mock(return_value=httpx.Response(status, content=b"Disallow: /"))
        async with httpx.AsyncClient() as client:
            assert await website._fetch_robots(client, ROBOTS_URL) == (None, status)
