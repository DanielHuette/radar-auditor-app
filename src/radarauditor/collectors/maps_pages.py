import calendar
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from pydantic import HttpUrl

from radarauditor.collectors.identity import Identity, identity, same_business
from radarauditor.collectors.patterns import collapse_whitespace, parse_http_url
from radarauditor.collectors.snapshot import DataRoute, PlaceSnapshot

BROWSER: DataRoute = "browser"
DAYS_PER_UNIT = {
    "minute": 1 / 1440,
    "stunde": 1 / 24,
    "hour": 1 / 24,
    "tag": 1.0,
    "day": 1.0,
    "woche": 7.0,
    "week": 7.0,
    "monat": 30.44,
    "month": 30.44,
    "jahr": 365.25,
    "year": 365.25,
}
RELATIVE_GERMAN = re.compile(
    r"vor\s+(einer|einem|eine|ein|\d+)\s+(Minute|Stunde|Tag|Woche|Monat|Jahr)[a-z]*",
    re.IGNORECASE,
)
RELATIVE_ENGLISH = re.compile(
    r"\b(an?|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
STARS_PATTERN = re.compile(r"^\s*(\d(?:[,.]\d)?)\s+(?:Sterne?|stars?)\b", re.IGNORECASE)
REVIEW_COUNT_PATTERN = re.compile(r"([\d.]+)\s+(?:Rezensionen|Rezension|reviews?)\b", re.IGNORECASE)
PHOTO_COUNT_PATTERN = re.compile(r"(\d[\d.]*)\s+(?:Fotos|Foto|photos?)\b", re.IGNORECASE)
QUESTION_COUNT_PATTERN = re.compile(r"(\d+)\s+(?:Fragen|Frage|questions?)\b", re.IGNORECASE)
CHIJ_PATTERN = re.compile(r"ChIJ[0-9A-Za-z_-]{16,}")
FEATURE_ID_PATTERN = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
LABEL_PREFIXES = ("Adresse:", "Telefon:", "Address:", "Phone:")
SERVICE_AREA_PREFIXES = ("Einzugsgebiet:", "Servicegebiet:", "Service area:")
DESCRIPTION_HEADINGS = ("Von ", "Vom Inhaber", "Beschreibung", "From ")
SERVICE_HEADINGS = ("Leistungen", "Dienstleistungen", "Services")
PRODUCT_HEADINGS = ("Produkte", "Products")
POST_HEADINGS = ("Neuigkeiten", "Updates vom Inhaber", "Updates from the owner")
QUESTION_HEADINGS = ("Fragen und Antworten", "Questions & answers")
OWNER_REPLY_LABELS = ("Antwort vom Inhaber", "Response from the owner")
SPONSORED_LABELS = ("Gesponsert", "Anzeige", "Sponsored")
NO_REVIEWS_LABELS = ("Keine Rezensionen", "No reviews")
CLOSED_LABELS = {
    "Dauerhaft geschlossen": "CLOSED_PERMANENTLY",
    "Permanently closed": "CLOSED_PERMANENTLY",
    "Vorübergehend geschlossen": "CLOSED_TEMPORARILY",
    "Temporarily closed": "CLOSED_TEMPORARILY",
}
OPERATIONAL = "OPERATIONAL"
GERMAN_MONTHS = {
    "januar": 1,
    "jan": 1,
    "februar": 2,
    "feb": 2,
    "märz": 3,
    "mär": 3,
    "mrz": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "juni": 6,
    "jun": 6,
    "juli": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "dez": 12,
}
MONTH_YEAR_PATTERN = re.compile(
    r"(?<![\wäöüÄÖÜ])("
    + "|".join(sorted(GERMAN_MONTHS, key=len, reverse=True))
    + r")\.?\s+((?:19|20)\d{2})(?!\d)",
    re.IGNORECASE,
)
MAX_DATE_TEXT_CHARS = 40
PHOTO_BUTTONS = ("Fotos ansehen", "Alle Fotos ansehen", "See photos", "Foto von ")
GALLERY_TABS = (("Vom Inhaber", "By owner"), ("Alle", "All"))
NEXT_PHOTO_BUTTONS = ("Nächstes Foto", "Nächstes Bild", "Weiter", "Next photo", "Next")
STREET_VIEW_MARKERS = ("street view", "360°", "360 °", "fotokugel", "photo sphere")
GALLERY_TAB_LABEL = re.compile(r"street view\s*(?:&|und|and)\s*360", re.IGNORECASE)
VIEWER_EXCLUDED = '[role="tab"], [role="tablist"], [data-review-id]'
REVIEW_TEXT_CLASS = "wiI7pd"
REVIEW_DATE_CLASS = "rsqaWe"
REPLY_CLASS = "CDe7pd"
MAX_REPLY_CLIMB = 3


@dataclass(frozen=True)
class MapListing:
    name: str
    url: str
    place_id: str | None
    rating: float | None
    review_count: int | None


@dataclass(frozen=True)
class ViewerPhoto:
    taken: date | None
    street_view: bool


@dataclass(frozen=True)
class PhotoDates:
    newest: date | None
    dated_count: int


def photo_date(text: str, now: datetime) -> date | None:
    text = collapse_whitespace(text)
    if not text or len(text) > MAX_DATE_TEXT_CHARS:
        return None
    match = MONTH_YEAR_PATTERN.search(text)
    if match is not None:
        month = GERMAN_MONTHS[match.group(1).casefold()]
        year = int(match.group(2))
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        return min(last_day, now.date())
    age = relative_age_days(text)
    return (now - timedelta(days=age)).date() if age is not None else None


def page_texts(html: str) -> frozenset[str]:
    return frozenset(_viewer_texts(html))


def viewer_photo(html: str, before: frozenset[str], now: datetime) -> ViewerPhoto:
    texts = [text for text in _viewer_texts(html) if text not in before]
    taken = next((found for text in texts if (found := photo_date(text, now)) is not None), None)
    folded = " ".join(text for text in texts if not GALLERY_TAB_LABEL.search(text)).casefold()
    street_view = any(marker in folded for marker in STREET_VIEW_MARKERS)
    return ViewerPhoto(taken=taken, street_view=street_view)


def photo_button(html: str) -> str | None:
    labels = [_label(element) for element in BeautifulSoup(html, "lxml").select("[aria-label]")]
    return next(
        (name for name in PHOTO_BUTTONS if any(label.startswith(name) for label in labels)), None
    )


def gallery_tab(html: str) -> str | None:
    offered = tab_labels(html)
    for names in GALLERY_TABS:
        for name in names:
            if any(tab == name or tab.startswith(f"{name} ") for tab in offered):
                return name
    return None


def next_photo_button(html: str) -> str | None:
    labels = {_label(element) for element in BeautifulSoup(html, "lxml").select("[aria-label]")}
    return next((name for name in NEXT_PHOTO_BUTTONS if name in labels), None)


def newest_photo(photos: Iterable[ViewerPhoto]) -> PhotoDates:
    dates = [photo.taken for photo in photos if photo.taken is not None and not photo.street_view]
    return PhotoDates(newest=max(dates, default=None), dated_count=len(dates))


def relative_age_days(text: str) -> float | None:
    match = RELATIVE_GERMAN.search(text) or RELATIVE_ENGLISH.search(text)
    if match is None:
        return None
    amount_text, unit = match.group(1).lower(), match.group(2).lower()
    amount = int(amount_text) if amount_text.isdigit() else 1
    return amount * DAYS_PER_UNIT[unit]


def is_place_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    return soup.select_one('div[role="feed"]') is None and _main(soup).select_one("h1") is not None


def tab_labels(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    return [
        label
        for element in soup.select('[role="tab"]')
        if (label := _label(element) or collapse_whitespace(element.get_text(" ")))
    ]


def place_id_from(url: str, html: str = "") -> str | None:
    match = CHIJ_PATTERN.search(url) or CHIJ_PATTERN.search(html)
    if match is not None:
        return match.group(0)
    feature = FEATURE_ID_PATTERN.search(url) or FEATURE_ID_PATTERN.search(html)
    return feature.group(1) if feature is not None else None


def parse_listings(html: str) -> list[MapListing]:
    soup = BeautifulSoup(html, "lxml")
    feed = soup.select_one('div[role="feed"]')
    if feed is None:
        return []
    listings: list[MapListing] = []
    seen: set[str] = set()
    for anchor in feed.select('a[href*="/maps/place/"]'):
        url = _attribute(anchor, "href")
        container = anchor.find_parent(attrs={"role": "article"}) or anchor.parent or anchor
        name = _label(anchor) or _label(container)
        if not name or url in seen or _is_sponsored(container):
            continue
        seen.add(url)
        labels = list(_labels(container))
        review_count = _first_count(labels, REVIEW_COUNT_PATTERN)
        if review_count is None and any(
            collapse_whitespace(str(node)) in NO_REVIEWS_LABELS for node in _nodes(container)
        ):
            review_count = 0
        listings.append(
            MapListing(
                name=name,
                url=url,
                place_id=place_id_from(url),
                rating=_first_number(labels, STARS_PATTERN),
                review_count=review_count,
            )
        )
    return listings


def listing_identity(listing: MapListing) -> Identity:
    return identity(name=listing.name, place_ids=[listing.place_id])


def listing_position(listings: list[MapListing], own: Identity) -> int | None:
    for position, listing in enumerate(listings, start=1):
        if same_business(own, listing_identity(listing)):
            return position
    return None


def parse_place(
    overview_html: str,
    url: str,
    now: datetime,
    *,
    reviews_html: str | None = None,
    about_html: str | None = None,
    photos: PhotoDates | None = None,
) -> PlaceSnapshot:
    soup = BeautifulSoup(overview_html, "lxml")
    main = _main(soup)
    about = _main(BeautifulSoup(about_html, "lxml")) if about_html is not None else None
    values = _overview_values(soup, main, overview_html, url)
    values["description"] = _place_description(main, about)
    if reviews_html is not None:
        values["recent_reviews"] = parse_reviews(reviews_html, now)
    if about is not None:
        values["attributes"] = _attributes(about)
    if photos is not None:
        values["photo_newest_date"] = photos.newest
        values["photo_dated_count"] = photos.dated_count
    read = {field for field, value in values.items() if value is not None}
    return PlaceSnapshot.model_validate(
        {
            "types": [values["primary_type"]] if values["primary_type"] else [],
            "recent_reviews": [],
            **values,
            "value_routes": dict.fromkeys(sorted(read), BROWSER),
        }
    )


def _overview_values(
    soup: BeautifulSoup, main: Tag, overview_html: str, url: str
) -> dict[str, object]:
    canonical = soup.select_one('link[rel="canonical"]')
    page_address = _attribute(canonical, "href") if canonical is not None else ""
    labels = list(_labels(main))
    return {
        "place_id": place_id_from(f"{url} {page_address}", overview_html) or url,
        "name": _text(main.select_one("h1")),
        "formatted_address": _item_value(main, '[data-item-id="address"]'),
        "phone": _item_value(main, '[data-item-id^="phone:tel:"]') or None,
        "website": _website(main),
        "primary_type": _text(main.select_one('button[jsaction*="category"]')) or None,
        "rating": _first_number(labels, STARS_PATTERN),
        "review_count": _first_count(labels, REVIEW_COUNT_PATTERN) or 0,
        "has_opening_hours": _has_opening_hours(main),
        "photo_count": _photo_count(main, labels),
        "business_status": _status(main),
        "google_maps_uri": parse_http_url(url),
        "services": _section_items(main, SERVICE_HEADINGS),
        "products": _section_items(main, PRODUCT_HEADINGS),
        "service_areas": _service_areas(labels),
        "post_ages_days": _post_ages(main),
        "question_count": _question_count(main),
    }


def _place_description(main: Tag, about: Tag | None) -> str | None:
    description = _description(main) or (_description(about) if about is not None else None)
    if description is None and about is not None:
        return ""
    return description


def parse_reviews(html: str, now: datetime) -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "lxml")
    reviews: list[dict[str, object]] = []
    seen: set[str] = set()
    for block in soup.select("div[data-review-id][aria-label]"):
        review_id = _attribute(block, "data-review-id")
        if review_id in seen or block.find_parent(attrs={"data-review-id": True}) is not None:
            continue
        seen.add(review_id)
        reviews.append(_review(block, now))
    return reviews


def _review(block: Tag, now: datetime) -> dict[str, object]:
    reply_block = _reply_block(block)
    inside_reply = set(reply_block.find_all(True)) if reply_block is not None else set()
    own_parts = [element for element in block.find_all(True) if element not in inside_reply]
    relative = _relative_text(
        element for element in own_parts if REVIEW_DATE_CLASS in _classes(element)
    ) or _relative_text(own_parts)
    text = next(
        (_text(element) for element in own_parts if REVIEW_TEXT_CLASS in _classes(element)), ""
    )
    return {
        "publishTime": _moment(now, relative),
        "relativePublishTimeDescription": relative,
        "rating": _first_number(_labels(block), STARS_PATTERN),
        "text": text,
        "reviewReply": _reply(reply_block, now) if reply_block is not None else None,
    }


def _reply(block: Tag, now: datetime) -> dict[str, object]:
    relative = _relative_text(block.find_all(True))
    texts = [_text(element) for element in block.find_all(class_=REVIEW_TEXT_CLASS)]
    return {
        "text": next((text for text in texts if text), ""),
        "publishTime": _moment(now, relative),
        "relativePublishTimeDescription": relative,
    }


def _reply_block(block: Tag) -> Tag | None:
    explicit = block.find(class_=REPLY_CLASS)
    if isinstance(explicit, Tag):
        return explicit
    for node in block.find_all(string=True):
        if collapse_whitespace(str(node)) not in OWNER_REPLY_LABELS:
            continue
        current = node.parent
        for _ in range(MAX_REPLY_CLIMB):
            if current is None or current is block:
                break
            if current.find(class_=REVIEW_TEXT_CLASS) is not None:
                return current
            current = current.parent
    return None


def _relative_text(elements: Iterable[Tag]) -> str | None:
    for element in elements:
        for node in element.find_all(string=True, recursive=False):
            text = collapse_whitespace(str(node))
            if text and relative_age_days(text) is not None:
                return text
    return None


def _moment(now: datetime, relative: str | None) -> str | None:
    age = relative_age_days(relative) if relative else None
    return (now - timedelta(days=age)).isoformat() if age is not None else None


def _viewer_texts(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for element in soup.select(VIEWER_EXCLUDED):
        element.decompose()
    texts: list[str] = []
    for element in soup.find_all(True):
        if element.name in ("script", "style"):
            continue
        label = _label(element)
        if label and len(label) <= MAX_DATE_TEXT_CHARS:
            texts.append(label)
        for node in element.find_all(string=True, recursive=False):
            text = collapse_whitespace(str(node))
            if text and len(text) <= MAX_DATE_TEXT_CHARS:
                texts.append(text)
    return texts


def _main(soup: BeautifulSoup) -> Tag:
    main = soup.select_one('div[role="main"]')
    return main if main is not None else soup


def _description(scope: Tag) -> str | None:
    for heading, container in _sections(scope):
        if heading.startswith(DESCRIPTION_HEADINGS) and not heading.startswith(POST_HEADINGS):
            text = collapse_whitespace(container.get_text(" "))
            return collapse_whitespace(text.removeprefix(heading))
    return None


def _section_items(scope: Tag, headings: tuple[str, ...]) -> list[str]:
    for heading, container in _sections(scope):
        if heading in headings:
            return _items(container)
    return []


def _attributes(scope: Tag) -> list[str]:
    skipped = (*DESCRIPTION_HEADINGS, *SERVICE_HEADINGS, *PRODUCT_HEADINGS)
    attributes: list[str] = []
    for heading, container in _sections(scope):
        if heading.startswith(skipped):
            continue
        attributes.extend(item for item in _items(container) if item not in attributes)
    return attributes


def _post_ages(scope: Tag) -> list[int]:
    for heading, container in _sections(scope):
        if heading in POST_HEADINGS:
            ages = [
                round(age)
                for node in container.find_all(string=True)
                if (age := relative_age_days(collapse_whitespace(str(node)))) is not None
            ]
            return sorted(ages)
    return []


def _question_count(scope: Tag) -> int | None:
    for heading, container in _sections(scope):
        if heading in QUESTION_HEADINGS:
            counted = _first_count(_strings(container), QUESTION_COUNT_PATTERN)
            return counted if counted is not None else len(container.select("li"))
    return None


def _service_areas(labels: list[str]) -> list[str]:
    for label in labels:
        for prefix in SERVICE_AREA_PREFIXES:
            if label.startswith(prefix):
                return [
                    area for part in label.removeprefix(prefix).split(",") if (area := part.strip())
                ]
    return []


def _sections(scope: Tag) -> Iterator[tuple[str, Tag]]:
    for heading in scope.select("h2, h3, [role='heading']"):
        text = collapse_whitespace(heading.get_text(" "))
        if text and isinstance(heading.parent, Tag):
            yield text, heading.parent


def _items(container: Tag) -> list[str]:
    items: list[str] = []
    for element in container.select("li, [role='listitem']"):
        labelled = element.select_one("[aria-label]")
        text = (_label(labelled) if labelled is not None else "") or _text(element)
        if text and text not in items:
            items.append(text)
    return items


def _website(main: Tag) -> HttpUrl | None:
    link = main.select_one('a[data-item-id="authority"]')
    href = _attribute(link, "href") if link is not None else ""
    return parse_http_url(href) if href else None


def _has_opening_hours(main: Tag) -> bool:
    for row in main.select("tr"):
        first = row.find(["td", "th"])
        if first is not None and _text(first).startswith(WEEKDAYS):
            return True
    return any("Öffnungszeiten" in label and "Montag" in label for label in _labels(main))


def _photo_count(main: Tag, labels: list[str]) -> int:
    counted = _first_count([*labels, *_strings(main)], PHOTO_COUNT_PATTERN)
    if counted is not None:
        return counted
    return len({_label(button) for button in main.select('button[aria-label^="Foto"]')})


def _status(main: Tag) -> str:
    text = collapse_whitespace(main.get_text(" "))
    return next((status for label, status in CLOSED_LABELS.items() if label in text), OPERATIONAL)


def _item_value(main: Tag, selector: str) -> str:
    element = main.select_one(selector)
    if element is None:
        return ""
    value = _label(element) or _text(element)
    for prefix in LABEL_PREFIXES:
        value = value.removeprefix(prefix)
    return value.strip()


def _is_sponsored(container: Tag) -> bool:
    return any(collapse_whitespace(str(node)) in SPONSORED_LABELS for node in _nodes(container))


def _first_number(labels: Iterable[str], pattern: re.Pattern[str]) -> float | None:
    for label in labels:
        match = pattern.search(label)
        if match is not None:
            return float(match.group(1).replace(",", "."))
    return None


def _first_count(labels: Iterable[str], pattern: re.Pattern[str]) -> int | None:
    for label in labels:
        match = pattern.search(label)
        if match is not None:
            return int(match.group(1).replace(".", ""))
    return None


def _labels(scope: Tag) -> Iterator[str]:
    for element in [scope, *scope.select("[aria-label]")]:
        label = _label(element)
        if label:
            yield label


def _strings(scope: Tag) -> list[str]:
    return [text for node in _nodes(scope) if (text := collapse_whitespace(str(node)))]


def _nodes(scope: Tag) -> Iterator[NavigableString]:
    for node in scope.find_all(string=True):
        if isinstance(node, NavigableString) and not (
            isinstance(node.parent, Tag) and node.parent.name in ("script", "style")
        ):
            yield node


def _label(element: Tag | None) -> str:
    return collapse_whitespace(_attribute(element, "aria-label")) if element is not None else ""


def _text(element: Tag | None) -> str:
    return collapse_whitespace(element.get_text(" ")) if element is not None else ""


def _classes(element: Tag) -> list[str]:
    value = element.get("class")
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _attribute(tag: Tag | None, name: str) -> str:
    if tag is None:
        return ""
    value = tag.get(name)
    if value is None:
        return ""
    return value if isinstance(value, str) else " ".join(value)
