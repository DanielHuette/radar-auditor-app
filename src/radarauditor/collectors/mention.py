import re

from radarauditor.collectors.patterns import host_of, normalise_name, site_key

WORD_PATTERN = re.compile(r"[^\W_]+")
MAX_REORDERED_WORDS = 3


def _words(text: str) -> list[str]:
    return WORD_PATTERN.findall(text.casefold())


def name_forms(name: str) -> list[str]:
    words = _words(normalise_name(name))
    if not words:
        return []
    forms = [" ".join(words)]
    if 2 <= len(words) <= MAX_REORDERED_WORDS:
        forms.append(" ".join(reversed(words)))
        forms.append("".join(words))
    return forms


def mentions_business(
    text: str, citations: list[str], name: str, domain: str | None = None
) -> bool:
    spoken = f" {' '.join(_words(text))} "
    if any(f" {form} " in spoken for form in name_forms(name)):
        return True
    host = host_of(domain) if domain else None
    if host is None:
        return False
    key = site_key(host)
    cited = (host_of(citation) for citation in citations)
    return key in text.casefold() or any(
        item is not None and site_key(item) == key for item in cited
    )
