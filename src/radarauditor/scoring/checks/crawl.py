import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import cache
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml
from dateutil.parser import isoparse

from radarauditor.collectors.identity import fold
from radarauditor.collectors.snapshot import AgentFetch, RawAudit, WebsiteSnapshot
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    CheckReturn,
    content_pages,
    not_measurable,
    tokens,
    url_key,
)
from radarauditor.scoring.checks.robots import RobotsRules

HTTP_OK = 200
BLOCK_STATUSES = frozenset({401, 403})
RATE_LIMITED_STATUS = 429
SERVER_ERROR_FROM = 500
CLIENT_ERROR_FROM = 400
TECHNICAL_PARAMETERS_FILE = Path("config/technische_parameter.yaml")
SYSTEM_PATH_PATTERN = re.compile(
    r"/(wp-admin|wp-login\.php|wp-json|wp-includes|xmlrpc\.php|cgi-bin|admin|administrator|"
    r"login|logout|anmeldung|abmeldung|registrierung|warenkorb|kasse|cart|checkout|"
    r"mein-konto|my-account|account|suche|search|feed)(/|$)"
)
BLOCK_PAGE_PHRASES = (
    "access denied",
    "forbidden",
    "just a moment",
    "checking your browser",
    "captcha",
    "unusual traffic",
    "request blocked",
    "attention required",
    "zugriff verweigert",
    "zugriff gesperrt",
)
GENERIC_FILE_PATTERN = re.compile(
    r"^(img|dsc|dscn|dscf|image|bild|foto|photo|pic|picture|screenshot|unnamed|untitled|"
    r"download|file|p)[-_ ]?\d*$"
)
HEX_PATTERN = re.compile(r"^[0-9a-f_\-~]+$")
WORD_PATTERN = re.compile(r"[a-zäöüß]{3,}")
HASH_SUFFIX_PATTERN = re.compile(r"~mv\d+$")


def crawled(raw: RawAudit) -> WebsiteSnapshot | None:
    if raw.website is None or not content_pages(raw.website):
        return None
    return raw.website


def has_extended_data(website: WebsiteSnapshot) -> bool:
    return any(page.body_text is not None for page in website.pages)


def no_extended_data() -> CheckReturn:
    return not_measurable("crawl_data_outdated")


def start_key(website: WebsiteSnapshot) -> str:
    wanted = url_key(website.start_url)
    keys = [url_key(page.url) for page in website.pages]
    return wanted if wanted in keys else keys[0]


def gradation(affected: list[str], website: WebsiteSnapshot) -> CheckOutcome:
    if any(url_key(url) == start_key(website) for url in affected):
        return CheckOutcome.FAILED
    return CheckOutcome.PARTIAL if affected else CheckOutcome.PASSED


def worst(*outcomes: CheckOutcome) -> CheckOutcome:
    for candidate in (CheckOutcome.FAILED, CheckOutcome.PARTIAL):
        if candidate in outcomes:
            return candidate
    return CheckOutcome.PASSED


def origin_root(website: WebsiteSnapshot) -> str:
    parts = urlsplit(str(website.start_url))
    return f"{parts.scheme}://{parts.netloc}/"


@cache
def technical_parameters() -> tuple[frozenset[str], tuple[str, ...]]:
    document = yaml.safe_load(TECHNICAL_PARAMETERS_FILE.read_text(encoding="utf-8"))
    entries = document["parameters"]
    names = frozenset(entry["name"].casefold() for entry in entries if not entry.get("prefix"))
    prefixes = tuple(entry["name"].casefold() for entry in entries if entry.get("prefix"))
    return names, prefixes


def _is_technical_parameter(name: str) -> bool:
    names, prefixes = technical_parameters()
    return name in names or name.startswith(prefixes)


def is_system_path(url: str) -> bool:
    parts = urlsplit(url)
    parameters = {name.casefold() for name, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if any(_is_technical_parameter(name) for name in parameters):
        return True
    return SYSTEM_PATH_PATTERN.search(parts.path.casefold()) is not None


def robots_blocked_pages(
    website: WebsiteSnapshot, rules: RobotsRules, agent: str
) -> tuple[list[str], list[str]]:
    crawled = [str(page.url) for page in website.pages if not rules.allows(agent, str(page.url))]
    skipped = [
        url
        for url in dict.fromkeys(website.robots_skipped)
        if url not in crawled and not rules.allows(agent, url)
    ]
    content = [*crawled, *(url for url in skipped if not is_system_path(url))]
    return content, [url for url in skipped if is_system_path(url)]


def robots_unavailable(website: WebsiteSnapshot) -> CheckReturn | None:
    status = website.robots_status
    if website.robots_txt is not None:
        return None
    if status == RATE_LIMITED_STATUS:
        return not_measurable("robots_txt_rate_limited", robots_status=status)
    if status is not None and status >= SERVER_ERROR_FROM:
        return not_measurable("robots_txt_server_error", robots_status=status)
    if status is not None and CLIENT_ERROR_FROM <= status < SERVER_ERROR_FROM:
        return None
    return not_measurable("robots_txt_no_answer", robots_status=status)


def is_transient_status(status: int | None) -> bool:
    return status is None or status == RATE_LIMITED_STATUS or status >= SERVER_ERROR_FROM


def is_descriptive_filename(file_name: str) -> bool:
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    stem = HASH_SUFFIX_PATTERN.sub("", fold(stem))
    if len(stem) >= 8 and HEX_PATTERN.fullmatch(stem):
        return False
    if GENERIC_FILE_PATTERN.fullmatch(stem):
        return False
    return WORD_PATTERN.search(stem) is not None


def word_set(text: str) -> set[str]:
    return set(tokens(text))


def similarity(first: str, second: str) -> float:
    left, right = word_set(first), word_set(second)
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def agent_fetches(website: WebsiteSnapshot) -> dict[str, AgentFetch] | None:
    if website.agent_fetches is None:
        return None
    return {fetch.agent: fetch for fetch in website.agent_fetches}


def looks_like_block_page(text: str) -> bool:
    folded = fold(text)
    return any(phrase in folded for phrase in BLOCK_PAGE_PHRASES)


def duplicates(pairs: Iterable[tuple[str, str | None]]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for url, value in pairs:
        key = fold(value or "").strip()
        if key:
            seen.setdefault(key, []).append(url)
    return {key: urls for key, urls in seen.items() if len(urls) > 1}


def page_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = isoparse(value)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
