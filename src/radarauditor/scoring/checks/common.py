import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from urllib.parse import urlsplit

from pydantic import HttpUrl

from radarauditor.collectors.identity import Identity, fold, identity, is_name_search
from radarauditor.collectors.patterns import host_of
from radarauditor.collectors.snapshot import (
    PageSnapshot,
    PlaceSnapshot,
    RawAudit,
    SerpResult,
    WebsiteSnapshot,
)
from radarauditor.collectors.validation import result_problems
from radarauditor.db.models import CheckOutcome
from radarauditor.errors import ScoringError

CheckReturn = tuple[CheckOutcome, dict[str, object]]
Thresholds = Mapping[str, float | int | str]
CheckFunction = Callable[[RawAudit, Thresholds], CheckReturn]

TOLERANCE = 0.2
MAX_LISTED = 10
NO_PAID_DATA = "no_paid_data"
PROFILE_NOT_PROVABLE = "profile_not_provable"

POSTAL_CODE_PATTERN = re.compile(r"\b(\d{5})\b")
LEGAL_FORM_PATTERN = re.compile(
    r"\b(gmbh|ug|ag|kg|ohg|gbr|e\s?k|e\s?v|mbh|co|haftungsbeschraenkt|inh)\b"
)
UTILITY_PATH_PATTERN = re.compile(
    r"(impressum|datenschutz|privacy|agb|kontakt|contact|ueber-uns|uber-uns|about|team|"
    r"blog|ratgeber|magazin|news|aktuelles|sitemap|login|cookie)"
)
NAP_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("name", "company", "firma"),
    "street": ("street", "strasse", "address", "adresse"),
    "postal_code": ("postal_code", "postal", "zip", "plz"),
    "phone": ("phone", "telephone", "telefon", "tel"),
}
GOOGLE_PROFILE_MARKERS = (
    "google.com/maps",
    "google.de/maps",
    "maps.google",
    "g.page",
    "maps.app.goo.gl",
    "goo.gl/maps",
    "cid=",
)


def not_measurable(reason: str, **measured: object) -> CheckReturn:
    return CheckOutcome.NOT_MEASURABLE, {"reason": reason, **measured}


def route_entry(routes: Iterable[str | None]) -> dict[str, object]:
    found = sorted({route for route in routes if route})
    if not found:
        return {}
    if len(found) == 1:
        return {"route": found[0]}
    return {"routes": found}


def place_routes(places: Iterable[PlaceSnapshot], field: str) -> dict[str, object]:
    return route_entry(place.route_of(field) for place in places)


def dispatch(
    checks: Mapping[str, CheckFunction], check_id: str, raw: RawAudit, thresholds: Thresholds
) -> CheckReturn:
    check = checks.get(check_id)
    if check is None:
        raise ScoringError(f"No check function for {check_id}")
    return check(raw, thresholds)


def number(thresholds: Thresholds, key: str) -> float:
    value = thresholds.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScoringError(f"Threshold {key!r} is missing or not numeric")
    return float(value)


def band_outcome(value: float, pass_from: float, partial_from: float) -> CheckOutcome:
    if value >= pass_from:
        return CheckOutcome.PASSED
    if value >= partial_from:
        return CheckOutcome.PARTIAL
    return CheckOutcome.FAILED


def ceiling_outcome(value: float, pass_to: float, partial_to: float) -> CheckOutcome:
    if value <= pass_to:
        return CheckOutcome.PASSED
    if value <= partial_to:
        return CheckOutcome.PARTIAL
    return CheckOutcome.FAILED


def share_of_best(own: float, rivals: Mapping[str, float]) -> tuple[float, str | None]:
    if not rivals:
        return 1.0, None
    leader = max(rivals, key=lambda name: rivals[name])
    best = rivals[leader]
    if best <= 0:
        return 1.0, leader
    return round(own / best, 3), leader


def combine(outcomes: Iterable[CheckOutcome]) -> CheckOutcome:
    judged = [outcome for outcome in outcomes if outcome is not CheckOutcome.NOT_MEASURABLE]
    if not judged:
        return CheckOutcome.NOT_MEASURABLE
    if all(outcome is CheckOutcome.PASSED for outcome in judged):
        return CheckOutcome.PASSED
    if all(outcome is CheckOutcome.FAILED for outcome in judged):
        return CheckOutcome.FAILED
    return CheckOutcome.PARTIAL


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def same_site(url: str, host: str) -> bool:
    candidate = host_of(url)
    return candidate is not None and (candidate == host or candidate.endswith(f".{host}"))


def is_google_profile_link(link: str, profile_url: str | None) -> bool:
    if any(marker in link for marker in GOOGLE_PROFILE_MARKERS):
        return True
    return profile_url is not None and url_key(link) == url_key(profile_url)


def url_key(url: str | HttpUrl) -> str:
    parts = urlsplit(str(url))
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def url_path(url: str | HttpUrl) -> str:
    return fold(urlsplit(str(url)).path)


def listed(items: Iterable[str]) -> list[str]:
    return list(items)[:MAX_LISTED]


def content_pages(website: WebsiteSnapshot) -> list[PageSnapshot]:
    return [page for page in website.pages if 200 <= page.status_code < 300]


def start_page(website: WebsiteSnapshot) -> PageSnapshot | None:
    start = url_key(website.start_url)
    pages = content_pages(website)
    for page in pages:
        if url_key(page.url) == start:
            return page
    return pages[0] if pages else None


def find_page(website: WebsiteSnapshot, url: str | HttpUrl | None) -> PageSnapshot | None:
    if url is None:
        return None
    wanted = url_key(url)
    return next((page for page in website.pages if url_key(page.url) == wanted), None)


def click_depths(website: WebsiteSnapshot) -> dict[str, int]:
    links = {
        url_key(page.url): {url_key(link) for link in page.internal_links} for page in website.pages
    }
    root = url_key(website.start_url)
    depths = {root: 0}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        for target in links.get(current, set()):
            if target not in depths:
                depths[target] = depths[current] + 1
                queue.append(target)
    return depths


def is_utility_page(page: PageSnapshot, website: WebsiteSnapshot) -> bool:
    key = url_key(page.url)
    for legal in (website.legal_notice_url, website.privacy_url):
        if legal is not None and url_key(legal) == key:
            return True
    return UTILITY_PATH_PATTERN.search(url_path(page.url)) is not None


def h1_texts(page: PageSnapshot) -> list[str]:
    return [text for level, text in page.headings if level == 1]


def page_text(page: PageSnapshot) -> str:
    parts = [page.title or "", *(text for _, text in page.headings), page.text_excerpt]
    return fold(" ".join(parts))


def postal_code(text: str | None) -> str | None:
    if not text:
        return None
    match = POSTAL_CODE_PATTERN.search(text)
    return match.group(1) if match else None


def phone_key(phone: str | None) -> str | None:
    if not phone:
        return None
    stripped = phone.strip()
    digits = re.sub(r"\D", "", stripped)
    if stripped.startswith("+49"):
        digits = "0" + digits[2:]
    elif digits.startswith("0049"):
        digits = "0" + digits[4:]
    return digits or None


def street_key(street: str | None) -> str | None:
    if not street:
        return None
    folded = fold(street)
    folded = re.sub(r"(strasse|str\.|str\b)", "str", folded)
    key = re.sub(r"[^a-z0-9]", "", folded)
    return key or None


def address_key(address: str | None) -> str | None:
    if not address:
        return None
    code = postal_code(address)
    street = address.split(code)[0] if code else address.split(",")[0]
    street_part = street_key(street.strip(" ,"))
    if street_part is None and code is None:
        return None
    return f"{street_part or ''}|{code or ''}"


def normalise_name(name: str | None) -> str:
    if not name:
        return ""
    words = " ".join(tokens(name))
    return " ".join(LEGAL_FORM_PATTERN.sub(" ", words).split())


def names_match(left: str | None, right: str | None) -> bool:
    first, second = normalise_name(left), normalise_name(right)
    return bool(first) and bool(second) and (first in second or second in first)


def own_identity(raw: RawAudit) -> Identity:
    business = raw.business
    return identity(
        name=business_name(raw),
        place_ids=[raw.place.place_id] if raw.place is not None else [],
        website=customer_host(raw),
        phone=business.phone if business is not None else None,
        street=business.street if business is not None else None,
        postal_code=business.postal_code if business is not None else None,
    )


def business_name(raw: RawAudit) -> str | None:
    return (raw.business.name or None) if raw.business is not None else None


def business_city(raw: RawAudit) -> str | None:
    return (raw.business.city or None) if raw.business is not None else None


def customer_host(raw: RawAudit) -> str | None:
    if raw.website is not None:
        return host_of(raw.website.start_url)
    if raw.place is not None:
        return host_of(raw.place.website)
    return None


def google_web_serps(raw: RawAudit) -> list[SerpResult]:
    return [serp for serp in raw.serps if serp.engine == "google" and serp.vertical == "web"]


def usable(serps: Iterable[SerpResult]) -> list[SerpResult]:
    return [serp for serp in serps if not result_problems(serp)]


def serp_coverage(
    considered: Iterable[SerpResult], used: Iterable[SerpResult]
) -> dict[str, object]:
    used_phrases = {serp.phrase for serp in used}
    missing = [serp.phrase for serp in considered if serp.phrase not in used_phrases]
    return {"phrases_evaluated": len(used_phrases), "phrases_missing": listed(missing)}


def brand_serps(raw: RawAudit) -> list[SerpResult]:
    name = business_name(raw)
    if name is None:
        return []
    return [serp for serp in google_web_serps(raw) if is_name_search(serp.phrase, name)]


def service_terms(raw: RawAudit) -> dict[str, list[str]]:
    city_tokens = set(tokens(business_city(raw) or ""))
    name = business_name(raw)
    phrases = [serp.phrase for serp in google_web_serps(raw)]
    phrases.extend(ranking.phrase for ranking in raw.map_rankings)
    terms: dict[str, list[str]] = {}
    for phrase in phrases:
        if (name is not None and is_name_search(phrase, name)) or phrase in terms:
            continue
        words = [word for word in tokens(phrase) if word not in city_tokens]
        if words:
            terms[phrase] = words
    return terms


def page_matches_terms(page: PageSnapshot, words: list[str]) -> bool:
    focus = " ".join([url_path(page.url), page.title or "", *h1_texts(page)])
    present = set(tokens(focus))
    return all(word in present or word in fold(focus) for word in words)


def service_pages(raw: RawAudit) -> list[PageSnapshot]:
    if raw.website is None:
        return []
    website = raw.website
    pages = [page for page in content_pages(website) if not is_utility_page(page, website)]
    terms = service_terms(raw)
    if not terms:
        start = start_page(website)
        return [page for page in pages if start is None or page.url != start.url]
    return [
        page for page in pages if any(page_matches_terms(page, words) for words in terms.values())
    ]


def schema_objects(json_ld: Iterable[object]) -> Iterator[dict[str, object]]:
    stack: list[object] = list(json_ld)
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            if "@type" in item:
                yield item
            for value in item.values():
                if isinstance(value, dict | list):
                    stack.append(value)


def schema_types(item: Mapping[str, object]) -> set[str]:
    value = item.get("@type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {entry for entry in value if isinstance(entry, str)}
    return set()


def page_schema_types(page: PageSnapshot) -> set[str]:
    return {kind for item in schema_objects(page.json_ld) for kind in schema_types(item)}


def string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [entry for key in ("@id", "url") if isinstance(entry := value.get(key), str)]
    if isinstance(value, list):
        return [text for entry in value for text in string_values(entry)]
    return []


def nap_value(nap: Mapping[str, str], field: str) -> str | None:
    filled = {
        key.casefold(): value.strip() for key, value in nap.items() if value and value.strip()
    }
    aliases = NAP_KEYS[field]
    exact = next((filled[alias] for alias in aliases if alias in filled), None)
    if exact is not None:
        return exact
    return next(
        (value for key, value in filled.items() if any(alias in key for alias in aliases)), None
    )
