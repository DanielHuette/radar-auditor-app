import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from radarauditor.collectors.browser import BrowserService, Geolocation, ensure_not_blocked
from radarauditor.collectors.identity import identity, same_business
from radarauditor.collectors.maps_pages import (
    MapListing,
    PhotoDates,
    ViewerPhoto,
    gallery_tab,
    is_place_page,
    listing_identity,
    newest_photo,
    next_photo_button,
    page_texts,
    parse_listings,
    parse_place,
    photo_button,
    tab_labels,
    viewer_photo,
)
from radarauditor.collectors.snapshot import PlaceSnapshot
from radarauditor.errors import CollectionError, ProfileNotFoundError

logger = logging.getLogger(__name__)

MAPS_URL = "https://www.google.com/maps"
SEARCH_URL = MAPS_URL + "/search/{query}?hl=de&gl=de"
SEARCH_AT_URL = MAPS_URL + "/search/{query}/@{latitude:.7f},{longitude:.7f},{zoom}z?hl=de&gl=de"
PLACE_ID_URL = MAPS_URL + "/place/?q=place_id:{place_id}&hl=de&gl=de"
SEARCH_ZOOM = 14
READY_SELECTOR = "div[role='main'] h1, div[role='feed'], div[role='main']"
READY_TIMEOUT_MS = 15000
TAB_TIMEOUT_MS = 8000
REVIEW_SELECTOR = "div[data-review-id]"
REVIEW_TABS = ("Rezensionen", "Reviews")
ABOUT_TABS = ("Info", "About")
OVERVIEW_TABS = ("Übersicht", "Overview")
SORT_BUTTONS = ("Rezensionen sortieren", "Sort reviews")
NEWEST_SELECTOR = ", ".join(
    f'[role="{role}"]:has-text("{label}")'
    for role in ("menuitemradio", "menuitem", "option")
    for label in ("Neueste", "Newest")
)
CONTROL_TIMEOUT_MS = 5000
REVIEW_SCROLLS = 2
SCROLL_REVIEWS_SCRIPT = (
    "(() => { const review = document.querySelector('div[data-review-id]');"
    " let pane = review && review.parentElement;"
    " while (pane && pane.scrollHeight <= pane.clientHeight) { pane = pane.parentElement; }"
    " if (pane) { pane.scrollBy(0, pane.scrollHeight); } })()"
)
PHOTO_LIMIT = 20
VIEWER_READY_SELECTOR = (
    '[role="tablist"], [aria-label="Nächstes Foto"], [aria-label="Next photo"], '
    '[aria-label="Nächstes Bild"]'
)
EXPAND_REVIEWS_SCRIPT = (
    "document.querySelectorAll('div[data-review-id] button[aria-expanded=\"false\"]')"
    ".forEach((button) => button.click())"
)


@dataclass(frozen=True)
class ProfileTarget:
    name: str
    address: str = ""
    place_id: str | None = None
    url: str | None = None
    with_photos: bool = False

    def start_url(self) -> str:
        if self.url:
            return self.url
        if self.place_id and self.place_id.startswith("ChIJ"):
            return PLACE_ID_URL.format(place_id=quote(self.place_id, safe=""))
        query = f"{self.name} {self.address}".strip()
        return SEARCH_URL.format(query=quote_plus(query))


async def read_profile(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
    async with browser.page() as page:
        html = await _load(browser, page, target.start_url())
        if not is_place_page(html):
            listing = _matching_listing(parse_listings(html), target)
            await browser.linger(page)
            html = await _load(browser, page, listing.url)
            if not is_place_page(html):
                raise CollectionError(f"Google Maps shows no readable profile for {target.name}")
        url = page.url
        reviews_html = await _tab_or_none(_reviews(browser, page, html), "reviews")
        about_html = await _tab_or_none(
            _open_tab(browser, page, html, ABOUT_TABS, "div[role='main'] h2"), "info"
        )
        photos = (
            await _tab_or_none(_photos(browser, page, html), "photos")
            if target.with_photos
            else None
        )
    logger.info("Read the Google Maps profile of %s", target.name)
    return parse_place(
        html,
        url,
        datetime.now(UTC),
        reviews_html=reviews_html,
        about_html=about_html,
        photos=photos,
    )


async def search_listings(
    browser: BrowserService, phrase: str, at: Geolocation | None = None
) -> list[MapListing]:
    query = quote(phrase, safe="")
    url = (
        SEARCH_URL.format(query=query)
        if at is None
        else SEARCH_AT_URL.format(
            query=query, latitude=at.latitude, longitude=at.longitude, zoom=SEARCH_ZOOM
        )
    )
    async with browser.page(geolocation=at) as page:
        html = await _load(browser, page, url)
        if is_place_page(html):
            snapshot = parse_place(html, page.url, datetime.now(UTC))
            return [
                MapListing(
                    name=snapshot.name,
                    url=page.url,
                    place_id=snapshot.place_id,
                    rating=snapshot.rating,
                    review_count=snapshot.review_count,
                )
            ]
        await browser.linger(page)
        return parse_listings(await _content(page))


def _matching_listing(listings: list[MapListing], target: ProfileTarget) -> MapListing:
    own = identity(name=target.name, place_ids=[target.place_id])
    for listing in listings:
        if same_business(own, listing_identity(listing)):
            return listing
    raise ProfileNotFoundError(f"Google Maps lists no profile for {target.name}")


async def _load(browser: BrowserService, page: Page, url: str) -> str:
    await browser.open(page, url)
    await _wait(page, READY_SELECTOR, READY_TIMEOUT_MS)
    return await _content(page)


async def _content(page: Page) -> str:
    html = await page.content()
    ensure_not_blocked(page.url, html)
    return html


async def _wait(page: Page, selector: str, timeout_ms: int) -> None:
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
    except PlaywrightError as error:
        logger.debug("Waited in vain for %s: %s", selector, error.message)


async def _tab_or_none[T](reading: Awaitable[T | None], tab: str) -> T | None:
    try:
        return await reading
    except PlaywrightError as error:
        logger.warning("Google Maps %s tab not read: %s", tab, error.message)
        return None


async def _reviews(browser: BrowserService, page: Page, html: str) -> str | None:
    opened = await _open_tab(browser, page, html, REVIEW_TABS, REVIEW_SELECTOR)
    if opened is None:
        return None
    await _sort_newest(page, opened)
    for _ in range(REVIEW_SCROLLS):
        await page.evaluate(SCROLL_REVIEWS_SCRIPT)
        await browser.linger(page)
    await page.evaluate(EXPAND_REVIEWS_SCRIPT)
    return await _content(page)


async def _open_tab(
    browser: BrowserService, page: Page, html: str, labels: tuple[str, ...], ready: str
) -> str | None:
    offered = tab_labels(html)
    label = next((label for label in labels if any(tab.startswith(label) for tab in offered)), None)
    if label is None:
        return None
    await page.get_by_role("tab", name=label).first.click()
    await _wait(page, ready, TAB_TIMEOUT_MS)
    await browser.linger(page)
    return await _content(page)


async def _photos(browser: BrowserService, page: Page, overview_html: str) -> PhotoDates | None:
    now = datetime.now(UTC)
    opened = await _open_viewer(browser, page, overview_html)
    if opened is None:
        return None
    viewer, before = opened
    seen = await _read_viewer(browser, page, viewer, before, now)
    photos = newest_photo(seen)
    logger.info(
        "Read %d photos in the Google Maps viewer, %d with a date", len(seen), photos.dated_count
    )
    return photos


async def _open_viewer(
    browser: BrowserService, page: Page, overview_html: str
) -> tuple[str, frozenset[str]] | None:
    current = await _content(page)
    if photo_button(current) is None and photo_button(overview_html) is not None:
        current = await _open_tab(browser, page, current, OVERVIEW_TABS, READY_SELECTOR) or current
    label = photo_button(current)
    if label is None:
        return None
    before = page_texts(current)
    await page.get_by_role("button", name=label).first.click(timeout=CONTROL_TIMEOUT_MS)
    await _wait(page, VIEWER_READY_SELECTOR, TAB_TIMEOUT_MS)
    await browser.rest()
    viewer = await _content(page)
    if viewer == current:
        logger.warning("Google Maps photo viewer did not open")
        return None
    tab = gallery_tab(viewer)
    if tab is not None:
        await page.get_by_role("tab", name=tab).first.click(timeout=CONTROL_TIMEOUT_MS)
        await _wait(page, VIEWER_READY_SELECTOR, TAB_TIMEOUT_MS)
        await browser.rest()
        viewer = await _content(page)
    return viewer, before


async def _read_viewer(
    browser: BrowserService, page: Page, viewer: str, before: frozenset[str], now: datetime
) -> list[ViewerPhoto]:
    seen = [viewer_photo(viewer, before, now)]
    while len(seen) < PHOTO_LIMIT:
        next_label = next_photo_button(viewer)
        if next_label is None:
            break
        await page.get_by_role("button", name=next_label).first.click(timeout=CONTROL_TIMEOUT_MS)
        await browser.rest()
        shown = await _content(page)
        if shown == viewer:
            break
        viewer = shown
        seen.append(viewer_photo(viewer, before, now))
    return seen


async def _sort_newest(page: Page, html: str) -> bool:
    button = next((label for label in SORT_BUTTONS if label in html), None)
    if button is None:
        return False
    try:
        await page.get_by_role("button", name=button).first.click(timeout=CONTROL_TIMEOUT_MS)
        await page.locator(NEWEST_SELECTOR).first.click(timeout=CONTROL_TIMEOUT_MS)
    except PlaywrightError as error:
        logger.warning("Google Maps reviews not sorted by newest: %s", error.message)
        return False
    await _wait(page, REVIEW_SELECTOR, TAB_TIMEOUT_MS)
    return True
