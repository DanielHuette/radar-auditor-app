import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from radarauditor.collectors.patterns import collapse_whitespace, site_key
from radarauditor.collectors.snapshot import SocialPlatform, SocialSource, WebsiteSnapshot

MAX_NAME_VARIANTS = 3
MIN_DOMAIN_STEM_CHARS = 3
SEARCH_SCOPES: dict[SocialPlatform, str] = {
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "linkedin_company": "linkedin.com/company",
    "linkedin_person": "linkedin.com/in",
    "x": "x.com",
}
PLATFORMS: tuple[SocialPlatform, ...] = tuple(SEARCH_SCOPES)
INSTAGRAM_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}")
X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
NAME_WORD = re.compile(r"[^\W_]+")
INSTAGRAM_RESERVED = frozenset(
    {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct", "about", "legal"}
    | {"developer", "web", "share", "challenge", "directory", "locations", "oauth"}
)
FACEBOOK_RESERVED = frozenset(
    {"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "policies", "legal"}
    | {"help", "login", "login.php", "watch", "events", "groups", "marketplace", "gaming"}
    | {"stories", "reel", "video", "photo", "photos", "permalink.php", "hashtag", "public"}
    | {"privacy", "about", "ads", "business", "l.php", "home.php", "story.php"}
)
X_RESERVED = frozenset(
    {"intent", "share", "home", "i", "search", "hashtag", "explore", "settings", "login", "tos"}
    | {"privacy", "about", "messages", "notifications", "compose", "signup", "widgets", "oauth"}
)
FACEBOOK_NESTED_PROFILE_SEGMENTS = {"p": 2, "pages": 3, "people": 3}
SITE_BUILDER_HANDLES = frozenset({"wix", "wix-com", "squarespace", "jimdo", "shopify", "wordpress"})
INSTAGRAM_DOMAINS = ("instagram.com",)
FACEBOOK_DOMAINS = ("facebook.com", "fb.com")
LINKEDIN_DOMAINS = ("linkedin.com",)
X_DOMAINS = ("x.com", "twitter.com")


@dataclass(frozen=True)
class AccountRef:
    platform: SocialPlatform
    handle: str
    url: str


def account_from_link(link: str) -> AccountRef | None:
    parts = urlsplit(link.strip())
    host = site_key(parts.hostname or "")
    segments = _path_segments(link)
    if _belongs_to(host, INSTAGRAM_DOMAINS):
        return _instagram(segments)
    if _belongs_to(host, FACEBOOK_DOMAINS):
        return _facebook(segments, parts.query)
    if _belongs_to(host, LINKEDIN_DOMAINS):
        return _linkedin(segments)
    if _belongs_to(host, X_DOMAINS):
        return _x(segments)
    return None


def _path_segments(link: str) -> list[str]:
    return [segment for segment in urlsplit(link.strip()).path.split("/") if segment]


def _belongs_to(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _instagram(segments: list[str]) -> AccountRef | None:
    if not segments or segments[0].lower() in INSTAGRAM_RESERVED:
        return None
    if not INSTAGRAM_HANDLE.fullmatch(segments[0]):
        return None
    handle = segments[0].lower()
    return AccountRef("instagram", handle, f"https://www.instagram.com/{handle}/")


def _facebook(segments: list[str], query: str) -> AccountRef | None:
    if not segments or segments[0].lower() in FACEBOOK_RESERVED:
        return None
    first = segments[0].lower()
    if first == "profile.php":
        identifier = parse_qs(query).get("id", [""])[0]
        if not identifier.isdigit():
            return None
        return AccountRef(
            "facebook", identifier, f"https://www.facebook.com/profile.php?id={identifier}"
        )
    depth = FACEBOOK_NESTED_PROFILE_SEGMENTS.get(first)
    if depth is not None:
        if len(segments) < depth:
            return None
        url = "https://www.facebook.com/" + "/".join([first, *segments[1:depth]])
        return AccountRef("facebook", segments[depth - 1].lower(), url)
    return AccountRef("facebook", first, f"https://www.facebook.com/{segments[0]}")


def _linkedin(segments: list[str]) -> AccountRef | None:
    if len(segments) < 2:
        return None
    kind, slug = segments[0].lower(), segments[1]
    if kind == "company":
        return AccountRef(
            "linkedin_company", slug.lower(), f"https://www.linkedin.com/company/{slug}"
        )
    if kind == "in":
        return AccountRef("linkedin_person", slug.lower(), f"https://www.linkedin.com/in/{slug}")
    return None


def _x(segments: list[str]) -> AccountRef | None:
    if not segments or segments[0].lower() in X_RESERVED:
        return None
    if not X_HANDLE.fullmatch(segments[0]):
        return None
    handle = segments[0].lower()
    return AccountRef("x", handle, f"https://x.com/{handle}")


def linked_accounts(site: WebsiteSnapshot) -> dict[SocialPlatform, tuple[AccountRef, SocialSource]]:
    counts: Counter[AccountRef] = Counter()
    in_legal_notice: set[AccountRef] = set()
    for page in site.pages:
        for link in page.external_links:
            ref = account_from_link(str(link))
            if ref is None or ref.handle in SITE_BUILDER_HANDLES:
                continue
            counts[ref] += 1
            if page.url == site.legal_notice_url:
                in_legal_notice.add(ref)
    chosen: dict[SocialPlatform, tuple[AccountRef, SocialSource]] = {}
    for ref, _ in counts.most_common():
        if ref.platform not in chosen:
            source: SocialSource = "legal_notice_link" if ref in in_legal_notice else "website_link"
            chosen[ref.platform] = (ref, source)
    return chosen


def name_variants(names: Iterable[str | None], host: str) -> list[str]:
    stem = site_key(host).split(".")[0]
    candidates = [*names, stem if len(stem) >= MIN_DOMAIN_STEM_CHARS else None]
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = collapse_whitespace((candidate or "").replace('"', " "))
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            variants.append(cleaned)
    return variants[:MAX_NAME_VARIANTS]


def search_phrase(platform: SocialPlatform, variant: str) -> str:
    return f'site:{SEARCH_SCOPES[platform]} "{variant}"'


def accounts_from_results(platform: SocialPlatform, links: Iterable[str]) -> list[AccountRef]:
    found: dict[str, AccountRef] = {}
    for link in links:
        ref = account_from_link(link)
        if ref is None or ref.platform != platform:
            continue
        if len(_path_segments(link)) == len(_path_segments(ref.url)):
            found.setdefault(ref.handle, ref)
    return list(found.values())


def carries_name(variants: Iterable[str], name: str | None, handle: str) -> bool:
    name_words = NAME_WORD.findall((name or "").casefold())
    written = ("".join(name_words), "".join(NAME_WORD.findall(handle.casefold())))
    for variant in variants:
        words = NAME_WORD.findall(variant.casefold())
        if not words:
            continue
        if set(words) <= set(name_words) or any("".join(words) in text for text in written):
            return True
    return False
