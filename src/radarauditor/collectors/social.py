import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from pydantic import HttpUrl, SecretStr

from radarauditor.collectors import legal_notice
from radarauditor.collectors.apify import ServiceCostSink
from radarauditor.collectors.patterns import MIN_PHONE_DIGITS, host_of, normalise_phone
from radarauditor.collectors.snapshot import (
    BusinessFacts,
    SocialAccount,
    SocialGap,
    SocialPlatform,
    SocialSnapshot,
    SocialSource,
    SocialStatus,
    WebsiteSnapshot,
)
from radarauditor.collectors.social_links import (
    PLATFORMS,
    AccountRef,
    accounts_from_results,
    carries_name,
    linked_accounts,
    name_variants,
    search_phrase,
)
from radarauditor.collectors.social_profiles import (
    POSTS_ROUTES,
    Reading,
    read_posts,
    read_profile,
)
from radarauditor.collectors.social_search import search_links
from radarauditor.collectors.transport import USER_AGENT
from radarauditor.collectors.website import crawl_website
from radarauditor.config import Settings
from radarauditor.errors import CollectionError, ConfigurationError

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 2
TEL_PREFIX = "tel:"
PROFILE_NOT_AVAILABLE = "profile not available"
NO_RESULT_PAGE = "Google answered without a result page"
NO_NAME_TO_SEARCH = "no name to search for"
POSTS_NOT_READ = "posts not read"
LINK_PROOFS: dict[SocialSource, str] = {
    "website_link": "linked from the website",
    "legal_notice_link": "linked from the legal notice",
}


@dataclass(frozen=True)
class _Target:
    domain: str
    variants: list[str]
    city: str | None
    phones: frozenset[str]


async def measure_website(
    settings: Settings, start_url: HttpUrl, names: Sequence[str], max_pages: int
) -> SocialSnapshot:
    if settings.apify_token is None:
        raise ConfigurationError("APIFY_TOKEN is not set")
    site = await crawl_website(start_url, max_pages)
    return await collect_social(site, settings.apify_token, names)


async def collect_social(
    site: WebsiteSnapshot,
    token: SecretStr,
    names: Sequence[str] = (),
    city: str | None = None,
    phones: Sequence[str] = (),
    book: ServiceCostSink | None = None,
) -> SocialSnapshot:
    linked = linked_accounts(site)
    missing = [platform for platform in PLATFORMS if platform not in linked]
    gaps: list[SocialGap] = []
    async with httpx.AsyncClient(
        follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        accounts = [
            await _account(client, token, ref, source, None, book)
            for ref, source in linked.values()
        ]
        if missing:
            target = await _target(site, names, city, phones)
            found, gaps = await _search_accounts(client, token, missing, target, book)
            accounts.extend(found)
    accounts.sort(key=lambda account: PLATFORMS.index(account.platform))
    return SocialSnapshot(accounts=accounts, gaps=gaps, collected_at=datetime.now(UTC))


async def _target(
    site: WebsiteSnapshot,
    names: Sequence[str],
    city: str | None,
    phones: Sequence[str],
) -> _Target:
    legal = await _legal_notice_facts(site)
    domain = host_of(site.start_url) or ""
    written = [*phones, legal.phone]
    return _Target(
        domain,
        name_variants([*names, legal.name], domain),
        city or legal.city,
        frozenset(
            digits
            for digits in (_digits(phone) for phone in written if phone)
            if len(digits) >= MIN_PHONE_DIGITS
        ),
    )


async def _legal_notice_facts(site: WebsiteSnapshot) -> BusinessFacts:
    try:
        return await legal_notice.read_site(site)
    except CollectionError as error:
        logger.warning("Social accounts: the legal notice was not read: %s", error)
        return BusinessFacts()


async def _search_accounts(
    client: httpx.AsyncClient,
    token: SecretStr,
    missing: list[SocialPlatform],
    target: _Target,
    book: ServiceCostSink | None,
) -> tuple[list[SocialAccount], list[SocialGap]]:
    checked: dict[SocialPlatform, list[SocialAccount]] = {platform: [] for platform in missing}
    failures: dict[SocialPlatform, str] = (
        {} if target.variants else dict.fromkeys(missing, NO_NAME_TO_SEARCH)
    )
    unsettled = list(missing)
    for variant in target.variants:
        if not unsettled:
            break
        phrases = {platform: search_phrase(platform, variant) for platform in unsettled}
        try:
            links = await search_links(client, token, list(phrases.values()), book)
        except CollectionError as error:
            failures.update(dict.fromkeys(unsettled, str(error)))
            break
        for platform, phrase in phrases.items():
            if phrase not in links:
                failures[platform] = NO_RESULT_PAGE
                continue
            failures.pop(platform, None)
            candidates = accounts_from_results(platform, links[phrase])
            await _check_candidates(client, token, candidates, target, checked[platform], book)
            if _settled(checked[platform]):
                unsettled.remove(platform)
    accounts: list[SocialAccount] = []
    gaps: list[SocialGap] = []
    for platform in missing:
        confirmed = [account for account in checked[platform] if account.status == "confirmed"]
        accounts.extend(confirmed or checked[platform])
        if confirmed:
            continue
        if platform in failures:
            gaps.append(
                SocialGap(platform=platform, reason="search_failed", detail=failures[platform])
            )
        elif not checked[platform]:
            gaps.append(SocialGap(platform=platform, reason="no_account_found"))
    return accounts, gaps


async def _check_candidates(
    client: httpx.AsyncClient,
    token: SecretStr,
    candidates: list[AccountRef],
    target: _Target,
    checked: list[SocialAccount],
    book: ServiceCostSink | None,
) -> None:
    for ref in candidates:
        if _settled(checked):
            return
        if all(account.handle != ref.handle for account in checked):
            checked.append(await _account(client, token, ref, "google_search", target, book))


def _settled(checked: list[SocialAccount]) -> bool:
    return len(checked) >= MAX_CANDIDATES or any(
        account.status == "confirmed" for account in checked
    )


async def _account(
    client: httpx.AsyncClient,
    token: SecretStr,
    ref: AccountRef,
    source: SocialSource,
    target: _Target | None,
    book: ServiceCostSink | None = None,
) -> SocialAccount:
    try:
        reading = await read_profile(client, token, ref, book)
    except CollectionError as error:
        return _identity(ref, source, "unreachable", str(error))
    if reading is None:
        return _identity(ref, source, "unreachable", PROFILE_NOT_AVAILABLE)
    if target is None:
        proof = LINK_PROOFS[source]
    else:
        found = _proof(reading, ref, target)
        if found is None:
            return _identity(ref, source, "unsure", _no_proof(target))
        proof = found
    posts = reading.measurement.posts
    reason: str | None = None
    if ref.platform in POSTS_ROUTES:
        try:
            posts = await read_posts(client, token, ref, book)
        except CollectionError as error:
            reason = f"{POSTS_NOT_READ}: {error}"
    measurement = reading.measurement.model_copy(update={"posts": posts})
    return SocialAccount(
        **measurement.model_dump(),
        platform=ref.platform,
        url=ref.url,
        handle=ref.handle,
        source=source,
        status="confirmed",
        proof=proof,
        reason=reason,
        evidence_websites=reading.evidence.websites,
        evidence_phones=reading.evidence.phones,
        evidence_places=reading.evidence.places,
    )


def _identity(
    ref: AccountRef, source: SocialSource, status: SocialStatus, reason: str
) -> SocialAccount:
    return SocialAccount(
        platform=ref.platform,
        url=ref.url,
        handle=ref.handle,
        source=source,
        status=status,
        reason=reason,
    )


def _no_proof(target: _Target) -> str:
    return (
        f"the profile does not link to {target.domain}, shows no matching phone number "
        "and does not carry the name together with the place"
    )


def _digits(phone: str) -> str:
    return normalise_phone(phone.removeprefix(TEL_PREFIX))


def _proof(reading: Reading, ref: AccountRef, target: _Target) -> str | None:
    evidence = reading.evidence
    if any(host_of(website) == target.domain for website in evidence.websites):
        return f"the profile links to {target.domain}"
    if any(_digits(phone) in target.phones for phone in evidence.phones):
        return "the phone number matches"
    if (
        target.city is not None
        and _names_place(evidence.places, target.city)
        and carries_name(target.variants, reading.measurement.name, ref.handle)
    ):
        return f"the profile carries the name and names {target.city}"
    return None


def _names_place(texts: list[str], city: str) -> bool:
    whole_word = re.compile(rf"(?<!\w){re.escape(city.casefold())}(?!\w)")
    return any(whole_word.search(text.casefold()) for text in texts)
