import logging
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from radarauditor.collectors.browser import BrowserService
from radarauditor.collectors.patterns import (
    CITY,
    POSTAL_CITY_PATTERN,
    STREET_PATTERN,
    collapse_whitespace,
    find_phone,
    parse_http_url,
    site_key,
)
from radarauditor.collectors.snapshot import (
    BusinessFacts,
    LegalNotice,
    LegalNoticeCandidate,
    LegalNoticeWay,
    WebsiteSnapshot,
)
from radarauditor.collectors.transport import USER_AGENT, send
from radarauditor.errors import CollectionError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20
RENDER_SETTLE_MS = 8000
CLICK_TIMEOUT_MS = 5000
MAX_CLICKS = 4
MAX_FRAMES = 5
LEGAL_ELEMENT_ATTRIBUTE = "data-radar-legal"
MARK_LEGAL_ELEMENTS = """
([pattern, attribute]) => {
  const expression = new RegExp(pattern, "i");
  const maxText = 60;
  const selector = "button, [role=link], [role=button], [onclick], li, span, div";
  let count = 0;
  for (const element of document.querySelectorAll(selector)) {
    if (element.closest(`a[href], form, [${attribute}]`)) continue;
    const text = (element.innerText || "").trim();
    const label = element.getAttribute("aria-label") || "";
    const named = (text.length <= maxText && expression.test(text)) || expression.test(label);
    if (!named) continue;
    const clickable = element.matches("button, [role=link], [role=button], [onclick]")
      || getComputedStyle(element).cursor === "pointer";
    if (!clickable) continue;
    element.setAttribute(attribute, String(count));
    count += 1;
  }
  return count;
}
"""
DIRECT_PATHS = (
    "/impressum",
    "/impressum/",
    "/imprint",
    "/legal-notice",
    "/legal",
    "/anbieterkennzeichnung",
    "/kontakt",
    "/impressum.html",
    "/impressum.php",
)
MIN_MARKERS = 2
MIN_READABLE_CHARS = 30
MAX_NAME_CHARS = 120
MAX_BLOCK_LINES = 5
MAX_SECTION_LINES = 80
MAX_HEADING_CHARS = 40
PHONE_WINDOW_LINES = 12
LOWEST_HEADING_LEVEL = 7
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
LABEL_TAGS = ("p", "strong", "b", "span", "div")
INVISIBLE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")
CHROME_TAGS = ("header", "nav", "footer", "aside")

LINK_TEXT_PATTERN = re.compile(
    r"impressum|imprint|legal\s*notice|anbieterkennzeichnung|rechtliches|rechtliche\s+hinweise",
    re.IGNORECASE,
)
CONTACT_PATH_PATTERN = re.compile(r"kontakt|contact", re.IGNORECASE)
LEGAL_PATH_PATTERN = re.compile(r"impressum|imprint|legal|anbieterkennzeichnung", re.IGNORECASE)
LEGAL_TITLE_PATTERN = re.compile(
    r"impressum|imprint|legal\s*notice|anbieterkennzeichnung", re.IGNORECASE
)
SECTION_HEADING_PATTERN = re.compile(
    r"\W*(?:impressum|imprint|legal\s*notice|anbieterkennzeichnung)\W*", re.IGNORECASE
)
LEGAL_MARKERS = (
    re.compile(r"angaben\s+gem(?:ä|ae|a)(?:ß|ss)\s*§\s*5", re.IGNORECASE),
    re.compile(r"§\s*5\s*(?:ddg|tmg)", re.IGNORECASE),
    re.compile(r"verantwortlich\s+f(?:ü|ue)r\s+den\s+inhalt", re.IGNORECASE),
    re.compile(r"vertreten\s+durch", re.IGNORECASE),
    re.compile(r"registergericht", re.IGNORECASE),
    re.compile(r"umsatzsteuer-?\s*identifikationsnummer", re.IGNORECASE),
)
NAMED_PATTERN = re.compile(
    r"^(?:firma|firmenname|inhaber(?:in)?|unternehmen|name|anbieter|diensteanbieter|betreiber)"
    r"\s*:\s*(\S.*)$",
    re.IGNORECASE,
)
LABEL_PATTERN = re.compile(
    r"^(?:impressum|imprint|legal\s*notice|angaben|anbieter|diensteanbieter|herausgeber|"
    r"betreiber|verantwortlich|inhaltlich|v\.?\s?i\.?\s?s\.?\s?d\.?\s?p|kontakt|anschrift|"
    r"adresse|postanschrift|sitz\b|so erreichen|vertreten|vertretungsberechtigt|geschäftsführ|"
    r"register|handelsregister|amtsgericht|umsatzsteuer|ust|steuer|telefon|tel\b|fon\b|fax|"
    r"telefax|mobil|e-?mail|mail\b|internet|web|www\.|haftung|urheberrecht|streitschlichtung|"
    r"berufs|aufsichtsbehörde|kammer|bildnachweis|quellen|datenschutz)",
    re.IGNORECASE,
)
CONTACT_LABEL_PATTERN = re.compile(
    r"^(?:tel\b|telefon|fon\b|fax|telefax|mobil|e-?mail|mail\b|ust|umsatzsteuer|steuer|"
    r"register|handelsregister|amtsgericht|hrb\b|hra\b)",
    re.IGNORECASE,
)
LEADING_LABEL_PATTERN = re.compile(r"^([^:\d]{1,30}):\s*(\S.*)$")
POSTFACH_PATTERN = re.compile(r"^postfach\b", re.IGNORECASE)
COUNTRY_PREFIX_PATTERN = re.compile(r"(?:^|[\s,])(?:D|DE)\s*-?\s*$")
PART_SEPARATOR_PATTERN = re.compile(r"[,|·•]")
CITY_LINE_PATTERN = re.compile(rf"(?:D-?\s*)?({CITY})")
CONTACT_TEXT_PATTERN = re.compile(r"@|https?://|www\.", re.IGNORECASE)


def read_business(lines: list[str]) -> BusinessFacts:
    cleaned = [line for line in (collapse_whitespace(item) for item in lines) if line]
    for index, line in enumerate(cleaned):
        if CONTACT_LABEL_PATTERN.match(line):
            continue
        facts = _postal_block(cleaned, index, _without_label(line))
        if facts is not None:
            return facts
    return _block_without_postal_code(cleaned)


def _postal_block(lines: list[str], index: int, line: str) -> BusinessFacts | None:
    match = POSTAL_CITY_PATTERN.search(line)
    if match is None:
        return None
    prefix = COUNTRY_PREFIX_PATTERN.sub("", line[: match.start()]).strip(" ,|·•–-")
    parts = [part.strip() for part in PART_SEPARATOR_PATTERN.split(prefix) if part.strip()]
    street = _street(parts[-1]) if parts else None
    name_parts = [part for part in (parts[:-1] if street else parts) if not _is_postfach(part)]
    top = index
    if not parts and index > 0:
        above = lines[index - 1]
        street = _street(above)
        if street is not None or _is_postfach(above):
            top = index - 1
    line_name = _name_value(name_parts[0]) if name_parts else None
    return BusinessFacts(
        name=_block_name(lines, top) or line_name,
        street=street,
        postal_code=match.group(1),
        city=match.group(2),
        phone=find_phone("\n".join(lines[top : top + PHONE_WINDOW_LINES])),
    )


def _block_name(lines: list[str], top: int) -> str | None:
    name: str | None = None
    for line in reversed(lines[max(0, top - MAX_BLOCK_LINES) : top]):
        if _is_postfach(line):
            continue
        value = _name_value(line)
        if value is None:
            break
        name = value
    return name


def _block_without_postal_code(lines: list[str]) -> BusinessFacts:
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if _name_value(line) is not None and _street(line) is None
        ),
        None,
    )
    if start is None:
        return BusinessFacts(phone=find_phone("\n".join(lines[:PHONE_WINDOW_LINES])))
    block: list[str] = []
    for line in lines[start + 1 : start + 1 + MAX_BLOCK_LINES]:
        if _is_label(line) or _is_contact(line):
            break
        block.append(line)
    streets = [street for street in (_street(line) for line in block) if street]
    cities = [
        match.group(1)
        for match in (CITY_LINE_PATTERN.fullmatch(line) for line in block)
        if match is not None
    ]
    return BusinessFacts(
        name=_name_value(lines[start]),
        street=streets[0] if streets else None,
        city=cities[-1] if cities else None,
        phone=find_phone("\n".join(lines[start : start + PHONE_WINDOW_LINES])),
    )


def _name_value(line: str) -> str | None:
    named = NAMED_PATTERN.match(line)
    candidate = (named.group(1) if named else line).strip(" ,;")
    if not named and _is_label(line):
        return None
    if not candidate or len(candidate) > MAX_NAME_CHARS or _is_contact(candidate):
        return None
    if POSTAL_CITY_PATTERN.search(candidate) or "§" in candidate:
        return None
    return candidate


def _is_label(line: str) -> bool:
    return line.endswith(":") or bool(LABEL_PATTERN.match(line))


def _is_contact(text: str) -> bool:
    return bool(CONTACT_TEXT_PATTERN.search(text)) or find_phone(text) is not None


def _is_postfach(text: str) -> bool:
    return bool(POSTFACH_PATTERN.match(text.strip()))


def _without_label(line: str) -> str:
    labelled = LEADING_LABEL_PATTERN.match(line)
    return labelled.group(2) if labelled else line


def _street(text: str) -> str | None:
    segment = text.strip(" ,|·•–")
    if _is_postfach(segment):
        return None
    match = STREET_PATTERN.fullmatch(segment)
    return collapse_whitespace(match.group(1)) if match else None


def notice_lines(html: str) -> list[str]:
    return _main_lines(_soup(html))


@dataclass(frozen=True)
class PageScan:
    url: str
    html: str
    lines: list[str]
    labelled_way: LegalNoticeWay | None
    markers: int
    section: list[str] | None
    links: list[tuple[str, str]] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)


def scan_page(url: str, html: str) -> PageScan:
    soup = BeautifulSoup(html, "lxml")
    frames = _frame_sources(url, soup)
    _remove_invisible(soup)
    title = soup.title.get_text(" ") if soup.title is not None else ""
    first_heading = soup.find("h1")
    heading = first_heading.get_text(" ") if isinstance(first_heading, Tag) else ""
    labelled_way: LegalNoticeWay | None = None
    if LEGAL_PATH_PATTERN.search(urlsplit(url).path):
        labelled_way = "address"
    elif LEGAL_TITLE_PATTERN.search(f"{title} {heading}"):
        labelled_way = "heading"
    return PageScan(
        url=url,
        html=html,
        lines=_main_lines(_soup(html)),
        labelled_way=labelled_way,
        markers=_markers(soup.get_text(" ")),
        section=_heading_section(soup),
        links=_legal_links(url, soup),
        frames=frames,
    )


def _markers(text: str) -> int:
    return sum(1 for marker in LEGAL_MARKERS if marker.search(text))


def _soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    _remove_invisible(soup)
    return soup


def _remove_invisible(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(INVISIBLE_TAGS):
        tag.decompose()


def _frame_sources(url: str, soup: BeautifulSoup) -> list[str]:
    sources: list[str] = []
    for frame in soup.find_all(["iframe", "frame"], src=True):
        source = urldefrag(urljoin(url, str(frame["src"]))).url
        if urlsplit(source).scheme in {"http", "https"} and source not in sources:
            sources.append(source)
    return sources


def _main_lines(soup: BeautifulSoup) -> list[str]:
    main = soup.find("main") or soup.find(True, attrs={"role": "main"})
    root: Tag | BeautifulSoup = main if isinstance(main, Tag) else (soup.body or soup)
    if root is not main:
        for tag in root.find_all(CHROME_TAGS):
            tag.decompose()
    return _lines(root.get_text("\n"))


def _lines(text: str) -> list[str]:
    return [line for line in (collapse_whitespace(item) for item in text.splitlines()) if line]


def _legal_links(url: str, soup: BeautifulSoup) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        images = anchor.find_all("img")
        label = " ".join(
            [
                anchor.get_text(" "),
                str(anchor.get("title") or ""),
                str(anchor.get("aria-label") or ""),
                *(str(image.get("alt") or "") for image in images),
            ]
        )
        if not LINK_TEXT_PATTERN.search(label):
            continue
        target = urljoin(url, str(anchor["href"]))
        if urlsplit(target).scheme not in {"http", "https"}:
            continue
        address, fragment = urldefrag(target)
        links.append((address, fragment))
    return links


def _heading_section(soup: BeautifulSoup) -> list[str] | None:
    heading = soup.find(_is_section_heading)
    return _section_from(heading) if isinstance(heading, Tag) else None


def _is_section_heading(tag: Tag) -> bool:
    if tag.name not in HEADING_TAGS and tag.name not in LABEL_TAGS:
        return False
    if tag.find_parent("a") is not None or tag.find("a") is not None:
        return False
    text = collapse_whitespace(tag.get_text(" "))
    return len(text) <= MAX_HEADING_CHARS and bool(SECTION_HEADING_PATTERN.fullmatch(text))


def _section_by_id(html: str, fragment: str) -> list[str] | None:
    element = _soup(html).find(id=fragment)
    if not isinstance(element, Tag):
        return None
    if element.name in HEADING_TAGS or _is_section_heading(element):
        return _section_from(element)
    lines = _lines(element.get_text("\n"))
    return lines or None


def _section_from(heading: Tag) -> list[str]:
    level = _level(heading)
    inside = set(map(id, heading.descendants))
    texts: list[str] = []
    for element in heading.next_elements:
        if id(element) in inside:
            continue
        if isinstance(element, Tag) and element.name in HEADING_TAGS and _level(element) <= level:
            break
        if type(element) is NavigableString:
            texts.append(str(element))
    return _lines("\n".join(texts))[:MAX_SECTION_LINES]


def _level(tag: Tag) -> int:
    return int(tag.name[1]) if tag.name in HEADING_TAGS else LOWEST_HEADING_LEVEL


def _linked_lines(scan: PageScan, fragment: str) -> list[str]:
    if fragment:
        return _section_by_id(scan.html, fragment) or scan.section or scan.lines
    if scan.labelled_way is None and scan.section is not None:
        return scan.section
    return scan.lines


def _signalled_lines(scan: PageScan) -> list[str] | None:
    if scan.labelled_way is not None:
        return scan.lines
    if scan.section is not None:
        return scan.section
    if scan.markers >= MIN_MARKERS:
        return scan.lines
    return None


class PageBrowser(Protocol):
    async def render(self, url: str) -> str: ...

    async def click_through(self, url: str) -> list[tuple[str, str]]: ...


@dataclass(frozen=True)
class BrowserReader:
    browser: BrowserService

    async def render(self, url: str) -> str:
        try:
            async with self.browser.page() as page:
                await self._load(page, url)
                return await page.content()
        except PlaywrightError as error:
            raise CollectionError(f"{url} not rendered: {error.message}") from error

    async def click_through(self, url: str) -> list[tuple[str, str]]:
        opened: list[tuple[str, str]] = []
        try:
            async with self.browser.page() as page:
                count = await self._mark(page, url)
                for index in range(min(count, MAX_CLICKS)):
                    if index:
                        await self._mark(page, url)
                    opened.extend(await self._click(page, index))
        except PlaywrightError as error:
            raise CollectionError(f"{url} not explored: {error.message}") from error
        return opened

    async def _load(self, page: Page, url: str) -> None:
        await self.browser.open(page, url)
        await _settle(page)

    async def _mark(self, page: Page, url: str) -> int:
        await self._load(page, url)
        count = await page.evaluate(
            MARK_LEGAL_ELEMENTS, [LINK_TEXT_PATTERN.pattern, LEGAL_ELEMENT_ATTRIBUTE]
        )
        return int(count)

    async def _click(self, page: Page, index: int) -> list[tuple[str, str]]:
        popups: list[Page] = []

        def keep(popup: Page) -> None:
            popups.append(popup)

        page.context.on("page", keep)
        try:
            await page.click(f'[{LEGAL_ELEMENT_ATTRIBUTE}="{index}"]', timeout=CLICK_TIMEOUT_MS)
            await _settle(page)
        except PlaywrightError as error:
            logger.info("Legal notice element %s not clicked: %s", index, error.message)
            return []
        finally:
            page.context.remove_listener("page", keep)
        read = [(page.url, await page.content())]
        for popup in popups:
            await _settle(popup)
            read.append((popup.url, await popup.content()))
            await popup.close()
        return read


async def _settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=RENDER_SETTLE_MS)
    except PlaywrightError as error:
        logger.debug("Page still loading after %s ms: %s", RENDER_SETTLE_MS, error.message)


@dataclass
class _Candidate:
    url: str
    way: LegalNoticeWay
    lines: list[str] | None = None
    fragment: str = ""


async def locate(
    client: httpx.AsyncClient, scans: list[PageScan], browser: PageBrowser | None = None
) -> LegalNotice:
    if not scans:
        return LegalNotice()
    candidates = await _crawl_candidates(client, scans, browser)
    found = await _first_readable(candidates, browser)
    if found is None and browser is not None:
        explored = await _browser_candidates(client, scans, browser)
        candidates.extend(explored)
        found = await _first_readable(explored, browser)
    if found is None:
        direct = await _direct_candidates(client, scans[0].url)
        candidates.extend(direct)
        found = await _first_readable(direct, browser)
    listed = [item for item in map(_listed, candidates) if item is not None]
    if found is None:
        first = next((candidate for candidate in candidates if candidate.lines is not None), None)
        if first is None:
            return LegalNotice(candidates=listed)
        found = (first, first.lines or [], False)
    chosen, lines, rendered = found
    return LegalNotice(
        url=parse_http_url(chosen.url),
        way=chosen.way,
        rendered=rendered,
        lines=lines,
        candidates=listed,
    )


def _listed(candidate: _Candidate) -> LegalNoticeCandidate | None:
    url = parse_http_url(candidate.url)
    return LegalNoticeCandidate(url=url, way=candidate.way) if url is not None else None


async def _crawl_candidates(
    client: httpx.AsyncClient, scans: list[PageScan], browser: PageBrowser | None
) -> list[_Candidate]:
    by_url = {scan.url: scan for scan in scans}
    home_site = site_key(urlsplit(scans[0].url).hostname or "")
    dedicated = [
        _Candidate(scan.url, scan.labelled_way, scan.lines)
        for scan in scans
        if scan.labelled_way is not None
    ]
    taken = {candidate.url for candidate in dedicated}
    own: list[_Candidate] = []
    foreign: list[_Candidate] = []
    sections: list[_Candidate] = []
    for address, fragment in dict.fromkeys(link for scan in scans for link in scan.links):
        is_own = site_key(urlsplit(address).hostname or "") == home_site
        if is_own and fragment:
            sections.append(_Candidate(address, "section", fragment=fragment))
        elif address not in taken:
            taken.add(address)
            way: LegalNoticeWay = "link" if is_own else "foreign_link"
            (own if is_own else foreign).append(_Candidate(address, way, fragment=fragment))
    frame_pages = [scan for scan in scans if scan.labelled_way is not None]
    for candidate in [*own, *foreign, *sections]:
        target = by_url.get(candidate.url) or await _fetched_scan(client, candidate.url)
        if target is not None:
            candidate.lines = _linked_lines(target, candidate.fragment)
            frame_pages.append(target)
    texts: list[_Candidate] = []
    for scan in scans:
        if scan.labelled_way is None and scan.url not in taken and scan.section is not None:
            sections.append(_Candidate(scan.url, "section", scan.section))
            frame_pages.append(scan)
        if scan.markers >= MIN_MARKERS:
            texts.append(_Candidate(scan.url, "text", scan.lines))
    frames = await _frame_candidates(client, frame_pages, browser)
    return [*dedicated, *own, *foreign, *frames, *sections, *texts]


async def _frame_candidates(
    client: httpx.AsyncClient, pages: list[PageScan], browser: PageBrowser | None
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    seen: set[str] = set()
    for page in pages:
        for source in page.frames[:MAX_FRAMES]:
            if source in seen:
                continue
            seen.add(source)
            frame = await _fetched_scan(client, source)
            lines = frame.lines if frame is not None else []
            if not _readable(lines) and browser is not None:
                rendered = await _rendered_scan(browser, source)
                lines = rendered.lines if rendered is not None else []
            if _markers(" ".join(lines)) or read_business(lines).postal_code is not None:
                candidates.append(_Candidate(source, "frame", lines))
    return candidates


async def _browser_candidates(
    client: httpx.AsyncClient, scans: list[PageScan], browser: PageBrowser
) -> list[_Candidate]:
    contact = next(
        (scan.url for scan in scans[1:] if CONTACT_PATH_PATTERN.search(urlsplit(scan.url).path)),
        None,
    )
    candidates: list[_Candidate] = []
    for url in [scans[0].url, *([contact] if contact else [])]:
        try:
            html = await browser.render(url)
            clicked = await browser.click_through(url)
        except CollectionError as error:
            logger.warning("Legal notice not searched in the browser on %s: %s", url, error)
            continue
        rendered = scan_page(url, html)
        for address, fragment in dict.fromkeys(rendered.links):
            target = await _browser_scan(client, browser, address)
            if target is not None:
                linked = _linked_lines(target, fragment)
                candidates.append(_Candidate(address, "browser", linked, fragment))
        for page_url, page_html in [(url, html), *clicked]:
            signalled = _signalled_lines(scan_page(page_url, page_html))
            if signalled is not None:
                candidates.append(_Candidate(page_url, "browser", signalled))
    return candidates


async def _browser_scan(
    client: httpx.AsyncClient, browser: PageBrowser, url: str
) -> PageScan | None:
    fetched = await _fetched_scan(client, url)
    if fetched is not None and _readable(fetched.lines):
        return fetched
    return await _rendered_scan(browser, url) or fetched


async def _rendered_scan(browser: PageBrowser, url: str) -> PageScan | None:
    try:
        html = await browser.render(url)
    except CollectionError as error:
        logger.warning("Legal notice %s not rendered: %s", url, error)
        return None
    return scan_page(url, html)


async def _direct_candidates(client: httpx.AsyncClient, start_url: str) -> list[_Candidate]:
    parts = urlsplit(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates: list[_Candidate] = []
    for path in DIRECT_PATHS:
        scan = await _fetched_scan(client, f"{origin}{path}")
        lines = _signalled_lines(scan) if scan is not None else None
        if scan is not None and lines is not None:
            candidates.append(_Candidate(scan.url, "direct", lines))
    return candidates


async def _fetched_scan(client: httpx.AsyncClient, url: str) -> PageScan | None:
    try:
        response = await client.get(url)
    except httpx.HTTPError as error:
        logger.info("Legal notice candidate %s not read: %s", url, type(error).__name__)
        return None
    content_type = response.headers.get("content-type", "text/html").lower()
    if response.status_code != httpx.codes.OK or "html" not in content_type:
        return None
    return scan_page(urldefrag(str(response.url)).url, response.text)


async def _first_readable(
    candidates: list[_Candidate], browser: PageBrowser | None
) -> tuple[_Candidate, list[str], bool] | None:
    for candidate in candidates:
        if candidate.lines is None:
            continue
        lines, rendered = candidate.lines, False
        if not _readable(lines) and browser is not None:
            lines, rendered = await _rendered_lines(candidate, browser), True
        if read_business(lines) != BusinessFacts():
            return candidate, lines, rendered
    return None


async def _rendered_lines(candidate: _Candidate, browser: PageBrowser) -> list[str]:
    scan = await _rendered_scan(browser, candidate.url)
    if scan is None:
        return []
    if candidate.way == "section":
        return _linked_lines(scan, candidate.fragment) if candidate.fragment else scan.section or []
    return scan.lines


def _readable(lines: list[str]) -> bool:
    return sum(len(line) for line in lines) >= MIN_READABLE_CHARS


async def read_site(site: WebsiteSnapshot) -> BusinessFacts:
    if site.legal_notice is not None:
        return read_business(site.legal_notice.lines)
    if site.legal_notice_url is None:
        return BusinessFacts()
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response = await send(client, "GET", str(site.legal_notice_url), source="legal notice")
    return read_business(notice_lines(response.text))
