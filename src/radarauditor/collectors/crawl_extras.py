import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from radarauditor.collectors.page_parts import visible_main_text
from radarauditor.collectors.patterns import site_key
from radarauditor.collectors.snapshot import AgentFetch, PageSnapshot, SitemapEntry, UrlProbe

logger = logging.getLogger(__name__)

VISITOR = "visitor"
GOOGLEBOT = "googlebot"
BINGBOT = "bingbot"
OAI_SEARCHBOT = "oai-searchbot"
CLAUDE_SEARCHBOT = "claude-searchbot"
AGENT_USER_AGENTS: dict[str, str] = {
    VISITOR: (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    GOOGLEBOT: "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    BINGBOT: "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    OAI_SEARCHBOT: (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; "
        "+https://openai.com/searchbot"
    ),
    CLAUDE_SEARCHBOT: (
        "Mozilla/5.0 (compatible; Claude-SearchBot/1.0; "
        "+https://support.claude.com/en/articles/8896518)"
    ),
}
MAX_REDIRECT_HOPS = 5
SITEMAP_PROBE_SAMPLE = 20
CONCURRENT_PROBES = 4
SCHEMES = ("http", "https")
HOST_PREFIXES = ("", "www.")


@dataclass(frozen=True)
class Extras:
    agent_fetches: list[AgentFetch]
    variant_probes: list[UrlProbe]
    sitemap_probes: list[UrlProbe]


async def _fetch_as(
    client: httpx.AsyncClient, agent: str, url: str, delay_seconds: float
) -> AgentFetch:
    try:
        response = await client.get(url, headers={"User-Agent": AGENT_USER_AGENTS[agent]})
    except httpx.HTTPError as error:
        return AgentFetch(agent=agent, status=None, error=type(error).__name__)
    finally:
        await asyncio.sleep(delay_seconds)
    content_type = response.headers.get("content-type", "text/html").lower()
    text = visible_main_text(response.text) if "html" in content_type else ""
    return AgentFetch(agent=agent, status=response.status_code, text=text)


async def fetch_as_agents(
    client: httpx.AsyncClient, url: str, delay_seconds: float
) -> list[AgentFetch]:
    return [await _fetch_as(client, agent, url, delay_seconds) for agent in AGENT_USER_AGENTS]


async def probe_url(client: httpx.AsyncClient, url: str, delay_seconds: float) -> UrlProbe:
    chain: list[int] = []
    first_location: str | None = None
    current = url
    for _ in range(MAX_REDIRECT_HOPS):
        try:
            response = await client.get(current, follow_redirects=False)
        except httpx.HTTPError as error:
            return UrlProbe(
                url=url,
                status=chain[0] if chain else None,
                chain=chain,
                location=first_location,
                error=type(error).__name__,
            )
        finally:
            await asyncio.sleep(delay_seconds)
        chain.append(response.status_code)
        if response.is_redirect:
            current = urljoin(current, response.headers["location"])
            first_location = first_location or current
            continue
        return UrlProbe(
            url=url, status=chain[0], chain=chain, location=first_location, final_url=current
        )
    return UrlProbe(
        url=url, status=chain[0], chain=chain, location=first_location, error="too many redirects"
    )


async def probe_all(
    client: httpx.AsyncClient, urls: list[str], delay_seconds: float
) -> list[UrlProbe]:
    semaphore = asyncio.Semaphore(CONCURRENT_PROBES)

    async def one(url: str) -> UrlProbe:
        async with semaphore:
            return await probe_url(client, url, delay_seconds)

    return list(await asyncio.gather(*(one(url) for url in urls)))


def variant_urls(start_url: str, pages: list[PageSnapshot]) -> list[str]:
    parts = urlsplit(start_url)
    bare_host = site_key(parts.hostname or "")
    urls = [f"{scheme}://{prefix}{bare_host}/" for scheme in SCHEMES for prefix in HOST_PREFIXES]
    sample = next(
        (
            urlsplit(str(page.url))
            for page in pages
            if 200 <= page.status_code < 300
            and urlsplit(str(page.url)).path not in ("", "/")
            and not urlsplit(str(page.url)).query
        ),
        None,
    )
    if sample is not None:
        bare = f"{sample.scheme}://{sample.netloc}{sample.path.rstrip('/')}"
        urls.extend([bare, f"{bare}/"])
    return urls


def sitemap_sample(entries: list[SitemapEntry]) -> list[str]:
    step = max(1, len(entries) // SITEMAP_PROBE_SAMPLE)
    return [entry.url for entry in entries[::step][:SITEMAP_PROBE_SAMPLE]]


async def collect(
    client: httpx.AsyncClient,
    start_url: str,
    pages: list[PageSnapshot],
    sitemap_entries: list[SitemapEntry],
    delay_seconds: float,
) -> Extras:
    agent_fetches = await fetch_as_agents(client, start_url, delay_seconds)
    variants = await probe_all(client, variant_urls(start_url, pages), delay_seconds)
    sampled = await probe_all(client, sitemap_sample(sitemap_entries), delay_seconds)
    logger.info(
        "Probed %d address variants, %d sitemap entries, %d crawler identities",
        len(variants),
        len(sampled),
        len(agent_fetches),
    )
    return Extras(agent_fetches, variants, sampled)
