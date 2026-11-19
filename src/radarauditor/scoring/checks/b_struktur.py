import re
from datetime import datetime
from urllib.parse import urlsplit

from radarauditor.collectors.identity import fold
from radarauditor.collectors.snapshot import (
    LinkInfo,
    PageSnapshot,
    RawAudit,
    UrlProbe,
    WebsiteSnapshot,
)
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.a_technik import bing_same_page
from radarauditor.scoring.checks.common import (
    CheckFunction,
    CheckReturn,
    Thresholds,
    business_city,
    business_name,
    combine,
    content_pages,
    dispatch,
    h1_texts,
    is_utility_page,
    listed,
    names_match,
    normalise_name,
    not_measurable,
    phone_key,
    schema_objects,
    service_pages,
    service_terms,
    start_page,
    street_key,
    tokens,
    url_key,
)
from radarauditor.scoring.checks.crawl import (
    HTTP_OK,
    crawled,
    duplicates,
    gradation,
    has_extended_data,
    is_descriptive_filename,
    no_extended_data,
    page_moment,
)
from radarauditor.scoring.checks.robots import BINGBOT, directives_for

TEMPORARY_REDIRECTS = frozenset({302, 303, 307})
NOARCHIVE = "noarchive"
NOCACHE = "nocache"
NOSNIPPET = "nosnippet"
MIN_TOPIC_WORD = 4
STEM_LENGTH = 6
STOPWORDS = frozenset(
    {"und", "oder", "auch", "ihre", "ihr", "eine", "einer", "einem", "mehr", "nach", "unter"}
    | {"ueber", "fuer", "dein", "deine", "wird", "sind", "sich", "dass", "dann", "wenn", "aber"}
)
ORGANISATION_KEYS = ("address", "telephone")
SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]")
DIGITS_PATTERN = re.compile(r"\D")
PHONE_TAIL_DIGITS = 7


def _links_of(website: WebsiteSnapshot) -> list[tuple[str, LinkInfo]]:
    return [
        (str(page.url), link)
        for page in content_pages(website)
        for link in page.links or []
        if link.kind != "anchor"
    ]


def _link_identity(link: LinkInfo) -> tuple[str, str, str]:
    return link.kind, link.url or "", fold(link.text)


def _is_labelled(link: LinkInfo) -> bool:
    return bool(link.text or link.image_alt or link.aria_label)


def _navigation_check(website: WebsiteSnapshot) -> tuple[dict[str, object], list[str]] | None:
    items: dict[tuple[str, str], LinkInfo] = {}
    carrying_pages: list[str] = []
    for url, link in _links_of(website):
        if link.in_navigation:
            items.setdefault((fold(link.text), link.url or ""), link)
            if link.kind != "href":
                carrying_pages.append(url)
    if not items:
        return None
    broken = [link for link in items.values() if link.kind != "href"]
    return {
        "navigation_items": len(items),
        "navigation_without_href": listed(f"{link.kind}: {link.text}" for link in broken),
        "navigation_without_href_count": len(broken),
    }, list(dict.fromkeys(carrying_pages))


def reachable_by_link(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    start = start_page(website)
    start_key = url_key(start.url) if start is not None else None
    linked = {
        url_key(link)
        for page in website.pages
        for link in page.internal_links
        if url_key(link) != url_key(page.url)
    }
    not_linked = [
        str(page.url)
        for page in pages
        if url_key(page.url) != start_key and url_key(page.url) not in linked
    ]
    crawled_keys = {url_key(page.url) for page in website.pages}
    only_in_sitemap: list[str] = []
    if not website.crawl_capped:
        only_in_sitemap = [
            url
            for url in dict.fromkeys(website.sitemap_pages)
            if url_key(url) not in crawled_keys and url_key(url) not in linked
        ]
    measured: dict[str, object] = {
        "pages_not_linked": listed(not_linked),
        "pages_only_in_sitemap": listed(only_in_sitemap),
        "pages_unreachable_count": len(not_linked) + len(only_in_sitemap),
        "pages_total": len(pages) + len(only_in_sitemap),
        "navigation_measured": False,
    }
    navigation_pages: list[str] = []
    navigation = _navigation_check(website)
    if navigation is not None:
        navigation_measured, navigation_pages = navigation
        measured |= {**navigation_measured, "navigation_measured": True}
    affected = list(dict.fromkeys([*not_linked, *only_in_sitemap, *navigation_pages]))
    return gradation(affected, website), measured


def copilot_blocks(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    if all(page.body_text is None for page in pages):
        return no_extended_data()
    reasons: dict[str, list[str]] = {}
    for page in pages:
        found = directives_for(page.meta_robots_bing, BINGBOT) | directives_for(
            page.x_robots_tag, BINGBOT
        )
        page_reasons = [
            name
            for name in (NOARCHIVE, NOCACHE, NOSNIPPET)
            if name in found or (name == NOSNIPPET and page.has_data_nosnippet)
        ]
        if "max-snippet:0" in found and NOSNIPPET not in page_reasons:
            page_reasons.append("max-snippet:0")
        if page_reasons:
            reasons[str(page.url)] = page_reasons
    affected = list(reasons)
    return gradation(affected, website), {
        "pages_total": len(pages),
        "pages_affected": listed(affected),
        "pages_affected_count": len(affected),
        "pages_affected_reasons": dict(list(reasons.items())[:10]),
    }


def sitemap_clean(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if not website.sitemap_urls:
        return CheckOutcome.FAILED, {"sitemap_found": False}
    if website.sitemap_entries is None:
        return no_extended_data()
    entries = website.sitemap_entries
    if not entries:
        return not_measurable(
            "sitemap_not_readable", sitemap_urls=[str(u) for u in website.sitemap_urls]
        )
    if website.sitemap_probes is None:
        return not_measurable("no_sitemap_probes")
    canonical_of = {url_key(page.url): page for page in website.pages}
    problems: dict[str, str] = {}
    for probe in website.sitemap_probes:
        page = canonical_of.get(url_key(probe.url))
        if probe.status != HTTP_OK:
            problems[probe.url] = (
                f"Status {probe.status}" if probe.status else f"nicht abrufbar: {probe.error}"
            )
        elif page is not None and page.canonical is not None:
            if url_key(page.canonical) != url_key(page.url):
                problems[probe.url] = f"canonical zeigt auf {page.canonical}"
    without_lastmod = [entry.url for entry in entries if not entry.lastmod]
    affected = list(dict.fromkeys([*problems, *without_lastmod]))
    return gradation(affected, website), {
        "sitemap_found": True,
        "entries_total": len(entries),
        "entries_checked": len(website.sitemap_probes),
        "entries_with_problem": dict(list(problems.items())[:10]),
        "entries_with_problem_count": len(problems),
        "entries_with_lastmod": len(entries) - len(without_lastmod),
        "entries_without_lastmod": listed(without_lastmod),
        "entries_without_lastmod_count": len(without_lastmod),
    }


def index_notification(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return not_measurable("not_verifiable_from_outside")


def link_labels(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if all(page.links is None for page in content_pages(website)):
        return no_extended_data()
    unique = {_link_identity(link): (url, link) for url, link in _links_of(website)}
    unreadable = [(url, link) for url, link in unique.values() if link.kind != "href"]
    unlabelled = [(url, link) for url, link in unique.values() if not _is_labelled(link)]
    total = len(unique)
    if total == 0:
        return CheckOutcome.NOT_APPLICABLE, {"reason": "no_links"}
    affected = list(
        dict.fromkeys(
            url for url, link in _links_of(website) if link.kind != "href" or not _is_labelled(link)
        )
    )
    return gradation(affected, website), {
        "links_total": total,
        "pages_with_issues": listed(affected),
        "links_without_href": listed(
            f"{link.kind}: {link.text} ({url})" for url, link in unreadable
        ),
        "links_without_href_count": len(unreadable),
        "links_without_label": listed(f"{link.url} ({url})" for url, link in unlabelled),
        "links_without_label_count": len(unlabelled),
    }


def _exact(url: str) -> str:
    parts = urlsplit(url)
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path or '/'}{query}"


def _is_root(probe: UrlProbe) -> bool:
    return urlsplit(probe.url).path in ("", "/")


def single_address(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    probes = website.variant_probes
    if probes is None:
        return not_measurable("no_variant_probes")
    answered = [probe for probe in probes if probe.status is not None]
    if not answered:
        return not_measurable("no_variant_answered")
    preferred = {_exact(str(page.url)) for page in website.pages}
    duplicates_found = [
        probe
        for probe in answered
        if probe.status == HTTP_OK and _exact(probe.url) not in preferred
    ]
    temporary = [
        probe for probe in answered if probe.chain and probe.chain[0] in TEMPORARY_REDIRECTS
    ]
    if any(_is_root(probe) for probe in duplicates_found):
        outcome = CheckOutcome.FAILED
    elif duplicates_found or temporary:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.PASSED
    return outcome, {
        "variants_checked": len(answered),
        "duplicate_addresses": [probe.url for probe in duplicates_found],
        "temporary_redirects": [
            f"{probe.url} -> {probe.location} (Status {probe.chain[0]})" for probe in temporary
        ],
        "unreachable_variants": [probe.url for probe in probes if probe.status is None],
    }


def readable_without_scripts(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    if all(page.body_text is None for page in pages):
        return no_extended_data()
    empty = [
        str(page.url)
        for page in pages
        if not (page.body_text or "").split() and page.script_sources
    ]
    return gradation(empty, website), {
        "pages_total": len(pages),
        "pages_without_text": listed(empty),
        "pages_without_text_count": len(empty),
    }


def titles_and_descriptions(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    repeated_titles = duplicates((str(page.url), page.title) for page in pages)
    repeated_descriptions = duplicates((str(page.url), page.meta_description) for page in pages)
    shared_title = {url for urls in repeated_titles.values() for url in urls}
    shared_description = {url for urls in repeated_descriptions.values() for url in urls}
    reasons: dict[str, list[str]] = {}
    for page in pages:
        url = str(page.url)
        found = [
            reason
            for reason, applies in (
                ("no_title", not (page.title or "").strip()),
                ("no_description", not (page.meta_description or "").strip()),
                ("duplicate_title", url in shared_title),
                ("duplicate_description", url in shared_description),
            )
            if applies
        ]
        if found:
            reasons[url] = found
    return gradation(list(reasons), website), {
        "pages_total": len(pages),
        "pages_with_problem": listed(reasons),
        "problems_by_page": dict(list(reasons.items())[:10]),
        "duplicate_titles": {key: listed(urls) for key, urls in list(repeated_titles.items())[:5]},
    }


def _heading_problems(page: PageSnapshot) -> list[str]:
    levels = [level for level, _ in page.headings]
    problems: list[str] = []
    if 1 not in levels:
        problems.append("no_h1")
    previous = 0
    for level in levels:
        if previous and level > previous + 1:
            problems.append(f"h{previous}_to_h{level}")
            break
        previous = level
    return problems


def heading_structure(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    reasons = {str(page.url): found for page in pages if (found := _heading_problems(page))}
    return gradation(list(reasons), website), {
        "pages_total": len(pages),
        "pages_with_problem": listed(reasons),
        "problems_by_page": dict(list(reasons.items())[:10]),
    }


def image_names(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    if all(page.images is None for page in pages):
        return no_extended_data()
    shown = [(page, image) for page in pages for image in page.images or [] if not image.decorative]
    if not shown:
        return CheckOutcome.NOT_APPLICABLE, {"reason": "no_content_images"}
    without_alt = [(page, image) for page, image in shown if not (image.alt or "").strip()]
    bad_name = [
        (page, image)
        for page, image in shown
        if image.file_name is not None and not is_descriptive_filename(image.file_name)
    ]
    affected = list(dict.fromkeys(str(page.url) for page, _ in [*without_alt, *bad_name]))
    return gradation(affected, website), {
        "images_total": len(shown),
        "images_without_alt": len(without_alt),
        "images_with_generic_name": len(bad_name),
        "generic_names": listed(image.file_name or "" for _, image in bad_name),
        "pages_with_issues": listed(
            dict.fromkeys(str(page.url) for page, _ in [*without_alt, *bad_name])
        ),
    }


def _stems(text: str, excluded: set[str]) -> set[str]:
    return {
        token[:STEM_LENGTH]
        for token in tokens(text)
        if len(token) >= MIN_TOPIC_WORD and token not in STOPWORDS and token not in excluded
    }


def _excluded_words(raw: RawAudit) -> set[str]:
    return set(tokens(business_city(raw) or ""))


def one_topic(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = [page for page in content_pages(website) if not is_utility_page(page, website)]
    if not pages:
        return not_measurable("no_topic_pages")
    if all(page.body_text is None for page in pages):
        return no_extended_data()
    excluded = _excluded_words(raw)
    reasons: dict[str, str] = {}
    for page in pages:
        headline = " ".join(h1_texts(page))
        if not headline:
            reasons[str(page.url)] = "no_h1"
        elif not (page.title or "").strip():
            reasons[str(page.url)] = "no_title"
        elif not (
            _stems(headline, excluded)
            & _stems(page.title or "", excluded)
            & _stems(page.body_text or "", excluded)
        ):
            reasons[str(page.url)] = "no_common_topic"
    return gradation(list(reasons), website), {
        "pages_total": len(pages),
        "pages_with_problem": listed(reasons),
        "problems_by_page": dict(list(reasons.items())[:10]),
    }


def important_first(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    terms = service_terms(raw)
    if not terms:
        return not_measurable("no_search_phrases")
    city = business_city(raw)
    if city is None:
        return not_measurable("no_city")
    start = start_page(website)
    candidates = [*([start] if start is not None else []), *service_pages(raw)]
    pages = list({str(page.url): page for page in candidates}.values())
    if all(page.first_text is None for page in pages):
        return no_extended_data()
    folded_city = fold(city)
    missing: list[str] = []
    for page in pages:
        opening = fold(" ".join([*h1_texts(page), page.first_text or ""]))
        found = folded_city in opening and any(
            all(word in opening for word in words) for words in terms.values()
        )
        if not found:
            missing.append(str(page.url))
    return gradation(missing, website), {
        "pages_total": len(pages),
        "pages_without_service_and_city": listed(missing),
        "city": city,
    }


def content_freshness(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if not has_extended_data(website):
        return no_extended_data()
    modified_in_sitemap = {
        url_key(entry.url): entry.lastmod
        for entry in website.sitemap_entries or []
        if entry.lastmod
    }
    dates: dict[str, str] = {}
    moments: dict[str, datetime] = {}
    for page in content_pages(website):
        raw_date = page.last_modified or modified_in_sitemap.get(url_key(page.url))
        moment = page_moment(raw_date)
        if raw_date and moment is not None:
            dates[str(page.url)] = raw_date
            moments[str(page.url)] = moment
    if not dates:
        return not_measurable("no_dates")
    newest = max(moments, key=lambda url: moments[url])
    return CheckOutcome.PASSED, {
        "pages_dated": len(dates),
        "pages_total": len(content_pages(website)),
        "newest_change_date": dates[newest],
        "change_dates": dict(list(dates.items())[:10]),
    }


def _business_objects(page: PageSnapshot) -> list[dict[str, object]]:
    return [
        item
        for item in schema_objects(page.json_ld)
        if any(key in item for key in ORGANISATION_KEYS)
    ]


def _squash(text: str) -> str:
    return SEPARATOR_PATTERN.sub("", fold(text))


def _schema_claims(item: dict[str, object]) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    name, phone, address = item.get("name"), item.get("telephone"), item.get("address")
    if isinstance(name, str):
        claims.append(("name", name))
    if isinstance(phone, str):
        claims.append(("telephone", phone))
    if isinstance(address, dict):
        for field in ("streetAddress", "postalCode", "addressLocality"):
            value = address.get(field)
            if isinstance(value, str):
                claims.append((field, value))
    return claims


def _claim_found(field: str, value: str, site_text: str) -> bool:
    if field == "telephone":
        key = phone_key(value)
        digits = DIGITS_PATTERN.sub("", site_text)
        return key is not None and key[-PHONE_TAIL_DIGITS:] in digits
    if field == "streetAddress":
        key = street_key(value)
        return key is not None and key in _squash(re.sub(r"(?i)stra(ß|ss)e", "str", site_text))
    if field == "name":
        return normalise_name(value) in " ".join(tokens(site_text))
    return fold(value) in fold(site_text)


def schema_matches_text(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    if all(page.body_text is None for page in pages):
        return no_extended_data()
    site_text = " ".join(page.body_text or "" for page in pages)
    checked = 0
    mismatches: list[dict[str, str]] = []
    for page in pages:
        for item in _business_objects(page):
            for field, value in _schema_claims(item):
                checked += 1
                if not _claim_found(field, value, site_text):
                    mismatches.append({"page": str(page.url), "field": field, "value": value})
    if checked == 0:
        return CheckOutcome.NOT_APPLICABLE, {"reason": "no_structured_data"}
    affected = list(dict.fromkeys(mismatch["page"] for mismatch in mismatches))
    return gradation(affected, website), {
        "values_checked": checked,
        "pages_affected": listed(affected),
        "mismatches": mismatches[:10],
        "mismatch_count": len(mismatches),
    }


def consistent_name(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    name = business_name(raw)
    if name is None:
        return not_measurable("no_business_name")
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    candidates: dict[str, list[str]] = {}
    footer = website.footer_nap.get("name")
    if footer:
        candidates["footer"] = [footer]
    schema_names = [
        value
        for page in content_pages(website)
        for item in _business_objects(page)
        for field, value in _schema_claims(item)
        if field == "name"
    ]
    if schema_names:
        candidates["structured_data"] = list(dict.fromkeys(schema_names))
    site_name = next(
        (
            page.open_graph["og:site_name"]
            for page in website.pages
            if "og:site_name" in page.open_graph
        ),
        None,
    )
    if site_name:
        candidates["site_name"] = [site_name]
    start = start_page(website)
    if start is not None and start.title:
        candidates["title"] = [start.title]
    if not candidates:
        return not_measurable("no_name_sources")
    verdicts = {
        source: CheckOutcome.PASSED
        if all(names_match(name, value) for value in values)
        else CheckOutcome.FAILED
        for source, values in candidates.items()
    }
    return combine(verdicts.values()), {
        "business_name": name,
        "sources": candidates,
        "mismatching_sources": [
            source for source, verdict in verdicts.items() if verdict is CheckOutcome.FAILED
        ],
    }


CHECKS: dict[str, CheckFunction] = {
    "b3": titles_and_descriptions,
    "b4": heading_structure,
    "b5": reachable_by_link,
    "b10": copilot_blocks,
    "b11": sitemap_clean,
    "b12": index_notification,
    "b13": link_labels,
    "b14": single_address,
    "b15": readable_without_scripts,
    "b16": image_names,
    "b17": one_topic,
    "b18": important_first,
    "b19": content_freshness,
    "b20": schema_matches_text,
    "b21": consistent_name,
    "b22": bing_same_page,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
