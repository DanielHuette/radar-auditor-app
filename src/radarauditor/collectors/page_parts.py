import re
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from radarauditor.collectors.patterns import collapse_whitespace
from radarauditor.collectors.snapshot import ImageInfo, LinkInfo, LinkKind

INVISIBLE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")
MAX_LINKS_PER_PAGE = 500
BODY_TEXT_CHARS = 20000
FIRST_TEXT_CHARS = 600
MIN_FIRST_TEXT_WORDS = 5
TRACKING_PIXEL_EDGE = 1
DECORATIVE_ROLES = frozenset({"presentation", "none"})
NAVIGATION_TAGS = frozenset({"nav", "header"})
NAVIGATION_ROLES = frozenset({"navigation", "menubar", "menu"})
SCRIPT_LINK_ROLES = frozenset({"link", "menuitem", "button"})
CHROME_TAGS = ("nav", "header", "footer", "aside")
ROBOTS_BING_NAME_PATTERN = re.compile(r"^(robots|bingbot)$", re.IGNORECASE)


def attribute(tag: Tag, name: str) -> str | None:
    value = tag.get(name)
    if value is None:
        return None
    return value if isinstance(value, str) else " ".join(value)


def meta_robots_bing(soup: BeautifulSoup) -> str | None:
    contents = [
        (attribute(tag, "content") or "").strip()
        for tag in soup.find_all("meta", attrs={"name": ROBOTS_BING_NAME_PATTERN})
    ]
    return ", ".join(content for content in contents if content) or None


def has_data_nosnippet(soup: BeautifulSoup) -> bool:
    return soup.find(lambda tag: tag.has_attr("data-nosnippet")) is not None


def _inside_noscript(tag: Tag) -> bool:
    return any(parent.name == "noscript" for parent in tag.parents)


def _is_decorative(image: Tag) -> bool:
    if (attribute(image, "role") or "").casefold() in DECORATIVE_ROLES:
        return True
    if (attribute(image, "aria-hidden") or "").casefold() == "true":
        return True
    for edge in ("width", "height"):
        value = (attribute(image, edge) or "").strip()
        if value.isdigit() and int(value) <= TRACKING_PIXEL_EDGE:
            return True
    return False


def _file_name(source: str | None) -> str | None:
    if not source or source.startswith("data:"):
        return None
    name = unquote(urlsplit(source).path.rsplit("/", 1)[-1])
    return name or None


def images(soup: BeautifulSoup) -> list[ImageInfo]:
    found: list[ImageInfo] = []
    for image in soup.find_all("img"):
        if _inside_noscript(image):
            continue
        source = (attribute(image, "src") or attribute(image, "data-src") or "").strip() or None
        alt = attribute(image, "alt")
        found.append(
            ImageInfo(
                source=source,
                file_name=_file_name(source),
                alt=alt.strip() if alt is not None else None,
                decorative=_is_decorative(image),
            )
        )
    return found


def _in_navigation(tag: Tag) -> bool:
    for node in (tag, *tag.parents):
        if node.name in NAVIGATION_TAGS:
            return True
        if (attribute(node, "role") or "").casefold() in NAVIGATION_ROLES:
            return True
    return False


def _link_kind(anchor: Tag) -> LinkKind | None:
    href = attribute(anchor, "href")
    if href is None:
        has_label = bool(collapse_whitespace(anchor.get_text(" ")))
        scripted = attribute(anchor, "onclick") is not None
        role = (attribute(anchor, "role") or "").casefold() in SCRIPT_LINK_ROLES
        return "no_href" if has_label or scripted or role else None
    stripped = href.strip()
    lowered = stripped.casefold()
    if lowered.startswith("javascript:"):
        return "javascript"
    if not stripped or stripped.startswith("#"):
        return "anchor"
    return "href"


def _link_info(anchor: Tag, kind: LinkKind, base: str) -> LinkInfo:
    href = (attribute(anchor, "href") or "").strip()
    target = urldefrag(urljoin(base, href)).url if kind == "href" else None
    image = anchor.find("img")
    image_alt = (attribute(image, "alt") or "").strip() if isinstance(image, Tag) else ""
    return LinkInfo(
        kind=kind,
        text=collapse_whitespace(anchor.get_text(" ")),
        url=target,
        in_navigation=_in_navigation(anchor),
        image_alt=image_alt or None,
        aria_label=(attribute(anchor, "aria-label") or attribute(anchor, "title") or "").strip()
        or None,
    )


def links(soup: BeautifulSoup, base: str) -> list[LinkInfo]:
    found: list[LinkInfo] = []
    for anchor in soup.find_all("a"):
        kind = _link_kind(anchor)
        if kind is not None:
            found.append(_link_info(anchor, kind, base))
    for tag in soup.find_all(True):
        if tag.name == "a" or attribute(tag, "onclick") is None or not _in_navigation(tag):
            continue
        if tag.find_parent("a") is None and tag.find("a") is None:
            found.append(
                LinkInfo(
                    kind="onclick",
                    text=collapse_whitespace(tag.get_text(" ")),
                    in_navigation=True,
                    aria_label=(attribute(tag, "aria-label") or "").strip() or None,
                )
            )
    return found[:MAX_LINKS_PER_PAGE]


def _lines(root: Tag | BeautifulSoup) -> list[str]:
    parts = (collapse_whitespace(line) for line in root.get_text("\n").splitlines())
    return [part for part in parts if part]


def content_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    main = soup.find("main")
    if isinstance(main, Tag):
        return main
    body = soup.body
    if body is None:
        return soup
    copy = BeautifulSoup(str(body), "lxml")
    for tag in copy.find_all(CHROME_TAGS):
        tag.decompose()
    return copy


def first_text(root: Tag | BeautifulSoup) -> str | None:
    for paragraph in root.find_all("p"):
        text = collapse_whitespace(paragraph.get_text(" "))
        if len(text.split()) >= MIN_FIRST_TEXT_WORDS:
            return text[:FIRST_TEXT_CHARS]
    for line in _lines(root):
        if len(line.split()) >= MIN_FIRST_TEXT_WORDS:
            return line[:FIRST_TEXT_CHARS]
    return None


def main_text(root: Tag | BeautifulSoup) -> str:
    return " ".join(_lines(root))[:BODY_TEXT_CHARS]


def body_text(lines: list[str]) -> str:
    return " ".join(lines)[:BODY_TEXT_CHARS]


def visible_main_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(INVISIBLE_TAGS):
        tag.decompose()
    return main_text(content_root(soup))
