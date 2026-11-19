from bs4 import BeautifulSoup
from pydantic import HttpUrl

from radarauditor.collectors import page_parts, website

BASE = "https://www.beispiel-dach.de"

HTML = """<!doctype html>
<html lang="de"><head><title>Beispiel Dach</title>
<meta name="robots" content="index, follow">
<meta name="bingbot" content="noarchive">
<meta name="googlebot" content="nosnippet">
</head><body>
<header><nav>
<a href="/leistungen/">Leistungen</a>
<a href="javascript:void(0)">Menü</a>
<a>Ohne Ziel</a>
<span onclick="go()">Klick</span>
</nav></header>
<main>
<h1>Dachdecker in Münster</h1>
<p>Kurz.</p>
<p>Wir decken Dächer in Münster und Umgebung seit vielen Jahren zuverlässig.</p>
<img src="/bilder/Dach%20Muenster.jpg" alt="  Steildach in Münster ">
<img src="/bilder/IMG_0001.jpg">
<img src="/pixel.gif" width="1" height="1" alt="">
<img src="/deko.svg" role="presentation" alt="">
<img src="/versteckt.png" aria-hidden="true">
<img data-src="/lazy/team.jpg" alt="Team">
<img src="data:image/png;base64,AAAA" alt="Inline">
<noscript><img src="/noscript.jpg" alt="Ersatz"></noscript>
<a href="/kontakt/#formular" aria-label="Zum Kontakt"><img src="/k.png" alt="Brief"></a>
<a href="#oben">Nach oben</a>
<a href="">Leer</a>
<a href="https://www.facebook.com/x" title="Facebook-Seite"></a>
<div data-nosnippet>Interner Hinweis</div>
</main>
<footer><p>Impressum Datenschutz Dachdeckerei Beispiel Münster Telefon vorhanden</p></footer>
</body></html>"""


def _soup() -> BeautifulSoup:
    return BeautifulSoup(HTML, "lxml")


def test_images_keep_source_file_name_and_trimmed_alt() -> None:
    found = page_parts.images(_soup())
    first = found[0]
    assert (first.source, first.file_name, first.alt) == (
        "/bilder/Dach%20Muenster.jpg",
        "Dach Muenster.jpg",
        "Steildach in Münster",
    )


def test_an_image_without_alt_keeps_none_and_an_empty_alt_stays_empty() -> None:
    found = {image.file_name: image for image in page_parts.images(_soup())}
    assert found["IMG_0001.jpg"].alt is None
    assert found["pixel.gif"].alt == ""


def test_decorative_images_are_marked() -> None:
    found = {image.file_name: image.decorative for image in page_parts.images(_soup())}
    assert found["pixel.gif"] is True
    assert found["deko.svg"] is True
    assert found["versteckt.png"] is True
    assert found["Dach Muenster.jpg"] is False
    assert found["IMG_0001.jpg"] is False


def test_a_lazy_loaded_source_is_read_and_a_data_uri_has_no_file_name() -> None:
    found = page_parts.images(_soup())
    lazy = next(image for image in found if image.source == "/lazy/team.jpg")
    inline = next(image for image in found if (image.source or "").startswith("data:"))
    assert lazy.file_name == "team.jpg"
    assert inline.file_name is None


def test_images_inside_noscript_are_skipped() -> None:
    assert "noscript.jpg" not in [image.file_name for image in page_parts.images(_soup())]


def test_link_kinds() -> None:
    found = page_parts.links(_soup(), f"{BASE}/start/")
    by_text = {link.text: link for link in found}
    assert by_text["Leistungen"].kind == "href"
    assert by_text["Leistungen"].url == f"{BASE}/leistungen/"
    assert by_text["Menü"].kind == "javascript"
    assert by_text["Menü"].url is None
    assert by_text["Ohne Ziel"].kind == "no_href"
    assert by_text["Nach oben"].kind == "anchor"
    assert by_text["Leer"].kind == "anchor"


def test_a_link_url_loses_its_fragment() -> None:
    found = page_parts.links(_soup(), f"{BASE}/")
    contact = next(link for link in found if link.aria_label == "Zum Kontakt")
    assert contact.url == f"{BASE}/kontakt/"
    assert contact.image_alt == "Brief"
    assert contact.text == ""


def test_a_title_is_a_label_when_there_is_no_text() -> None:
    found = page_parts.links(_soup(), f"{BASE}/")
    facebook = next(link for link in found if link.url == "https://www.facebook.com/x")
    assert (facebook.text, facebook.aria_label) == ("", "Facebook-Seite")


def test_navigation_links_and_clickable_elements_are_marked() -> None:
    found = page_parts.links(_soup(), f"{BASE}/")
    navigation = {link.text: link.kind for link in found if link.in_navigation}
    assert navigation == {
        "Leistungen": "href",
        "Menü": "javascript",
        "Ohne Ziel": "no_href",
        "Klick": "onclick",
    }
    assert not any(link.in_navigation for link in found if link.aria_label == "Zum Kontakt")


def test_a_link_list_is_capped() -> None:
    anchors = "".join(f'<a href="/s{number}/">Seite {number}</a>' for number in range(600))
    found = page_parts.links(BeautifulSoup(f"<body>{anchors}</body>", "lxml"), BASE)
    assert len(found) == page_parts.MAX_LINKS_PER_PAGE


def test_the_bing_meta_reads_robots_and_bingbot_but_not_googlebot() -> None:
    assert page_parts.meta_robots_bing(_soup()) == "index, follow, noarchive"
    assert page_parts.meta_robots_bing(BeautifulSoup("<html></html>", "lxml")) is None


def test_data_nosnippet_is_detected() -> None:
    assert page_parts.has_data_nosnippet(_soup()) is True
    assert page_parts.has_data_nosnippet(BeautifulSoup("<p>Text</p>", "lxml")) is False


def test_first_text_is_the_first_paragraph_with_enough_words_in_the_main_part() -> None:
    root = page_parts.content_root(_soup())
    assert page_parts.first_text(root) == (
        "Wir decken Dächer in Münster und Umgebung seit vielen Jahren zuverlässig."
    )


def test_the_content_root_without_main_leaves_out_navigation_and_footer() -> None:
    html = (
        "<body><nav>Menüpunkt eins zwei drei vier</nav><p>Der eigentliche Text steht hier drin.</p>"
        "<footer>Fußzeile mit vielen Wörtern darin</footer></body>"
    )
    root = page_parts.content_root(BeautifulSoup(html, "lxml"))
    assert page_parts.main_text(root) == "Der eigentliche Text steht hier drin."


def test_first_text_falls_back_to_a_line_and_is_none_for_short_pages() -> None:
    root = BeautifulSoup("<body><div>Ein ganzer Satz ohne Absatz im Text.</div></body>", "lxml")
    assert page_parts.first_text(root) == "Ein ganzer Satz ohne Absatz im Text."
    assert page_parts.first_text(BeautifulSoup("<body><p>Zu kurz.</p></body>", "lxml")) is None


def test_the_visible_text_leaves_out_scripts_styles_and_chrome() -> None:
    html = (
        "<body><header>Kopf</header><main><h1>Titel</h1><script>var x = 1;</script>"
        "<p>Text im Hauptteil</p></main><footer>Fuß</footer></body>"
    )
    assert page_parts.visible_main_text(html) == "Titel Text im Hauptteil"


def test_the_body_text_is_cut_at_the_limit() -> None:
    assert len(page_parts.body_text(["wort"] * 10000)) == page_parts.BODY_TEXT_CHARS


def test_parse_page_fills_the_measured_values_of_the_crawl() -> None:
    page = website.parse_page(
        HttpUrl(f"{BASE}/start/"),
        200,
        HTML,
        x_robots_tag="googlebot: noarchive",
        last_modified="Wed, 30 Sep 2026 10:00:00 GMT",
    )
    assert page.meta_robots is not None
    assert page.meta_robots_bing == "index, follow, noarchive"
    assert page.x_robots_tag == "googlebot: noarchive"
    assert page.last_modified == "Wed, 30 Sep 2026 10:00:00 GMT"
    assert page.has_data_nosnippet is True
    assert page.body_text is not None
    assert "Dachdecker in Münster" in page.body_text
    assert page.first_text == (
        "Wir decken Dächer in Münster und Umgebung seit vielen Jahren zuverlässig."
    )
    assert page.images is not None
    assert len(page.images) == 8
    assert page.links is not None
    assert len(page.links) == 8


def test_parse_page_keeps_the_old_fields() -> None:
    page = website.parse_page(HttpUrl(f"{BASE}/start/"), 200, HTML)
    assert page.title == "Beispiel Dach"
    assert page.headings == [(1, "Dachdecker in Münster")]
