import re
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import HttpUrl

from radarauditor.collectors.patterns import host_of, normalise_phone
from radarauditor.collectors.snapshot import PlaceSnapshot

UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
NAME_WORD_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LEGAL_FORM_WORDS = frozenset(
    {"gmbh", "ug", "ag", "kg", "ohg", "gbr", "ek", "ev", "mbh", "co", "haftungsbeschraenkt"}
)
LEGAL_FORM_PAIRS = frozenset({("e", "k"), ("e", "v")})
OWNER_MARKER_WORDS = frozenset(
    {"inh", "inhaber", "inhaberin", "geschaeftsfuehrer", "geschaeftsfuehrerin"}
)
MIN_WORDS_FOR_PARTIAL_NAME = 2
POSTAL_CODE_PATTERN = re.compile(r"(?<!\d)(\d{5})(?!\d)")
STREET_SUFFIX_PATTERN = re.compile(r"strasse|str\b")
PLACE_ID_KIND_PREFIX = "ChIJ"
SHARED_HOSTS = frozenset(
    {
        "facebook.com",
        "m.facebook.com",
        "instagram.com",
        "linkedin.com",
        "xing.com",
        "google.com",
        "sites.google.com",
        "linktr.ee",
        "wa.me",
        "youtube.com",
        "tiktok.com",
    }
)


@dataclass(frozen=True)
class Identity:
    name: str | None = None
    place_ids: frozenset[str] = frozenset()
    host: str | None = None
    phone: str | None = None
    address: str | None = None


def fold(text: str) -> str:
    return text.casefold().translate(UMLAUTS)


def name_words(name: str | None) -> frozenset[str]:
    words = NAME_WORD_PATTERN.findall(fold((name or "").replace(".", "")))
    kept: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in OWNER_MARKER_WORDS:
            break
        pair = (word, words[index + 1]) if index + 1 < len(words) else None
        if pair in LEGAL_FORM_PAIRS:
            index += 2
            continue
        if word not in LEGAL_FORM_WORDS:
            kept.append(word)
        index += 1
    return frozenset(kept)


def names_agree(left: str | None, right: str | None) -> bool:
    first, second = name_words(left), name_words(right)
    if not first or not second:
        return False
    if first == second:
        return True
    smaller, larger = sorted((first, second), key=len)
    return len(smaller) >= MIN_WORDS_FOR_PARTIAL_NAME and smaller <= larger


def is_name_search(phrase: str | None, business_name: str | None) -> bool:
    return names_agree(phrase, business_name)


def _street_key(street: str) -> str | None:
    folded = STREET_SUFFIX_PATTERN.sub("str", fold(street))
    key = re.sub(r"[^a-z0-9]", "", folded)
    return key if re.search(r"\d", key) and re.search(r"[a-z]", key) else None


def address_key(
    *, street: str | None = None, postal_code: str | None = None, address: str | None = None
) -> str | None:
    if address:
        match = POSTAL_CODE_PATTERN.search(address)
        postal_code = postal_code or (match.group(1) if match else None)
        street = street or (address[: match.start()] if match else address.split(",")[0])
    code = POSTAL_CODE_PATTERN.search(postal_code or "")
    street_part = _street_key((street or "").strip(" ,")) if street else None
    if code is None or street_part is None:
        return None
    return f"{street_part}|{code.group(1)}"


def identity(
    *,
    name: str | None = None,
    place_ids: Iterable[str | None] = (),
    website: str | HttpUrl | None = None,
    phone: str | None = None,
    street: str | None = None,
    postal_code: str | None = None,
    address: str | None = None,
) -> Identity:
    digits = normalise_phone(phone) if phone else ""
    return Identity(
        name=name,
        place_ids=frozenset(item for item in place_ids if item),
        host=host_of(website),
        phone=digits or None,
        address=address_key(street=street, postal_code=postal_code, address=address),
    )


def place_identity(place: PlaceSnapshot) -> Identity:
    return identity(
        name=place.name,
        place_ids=[place.place_id],
        website=place.website,
        phone=place.phone,
        address=place.formatted_address,
    )


def _place_id_kind(place_id: str) -> str:
    return "place" if place_id.startswith(PLACE_ID_KIND_PREFIX) else "feature"


def _place_ids_decide(own: Identity, other: Identity) -> bool | None:
    shared_kinds = {_place_id_kind(item) for item in own.place_ids} & {
        _place_id_kind(item) for item in other.place_ids
    }
    if not shared_kinds:
        return None
    return any(
        item in other.place_ids for item in own.place_ids if _place_id_kind(item) in shared_kinds
    )


def is_shared_host(host: str) -> bool:
    return any(host == shared or host.endswith(f".{shared}") for shared in SHARED_HOSTS)


def _own_host(host: str | None) -> str | None:
    return None if host is None or is_shared_host(host) else host


def compare_features(own: Identity, other: Identity) -> bool | None:
    by_place_id = _place_ids_decide(own, other)
    if by_place_id is not None:
        return by_place_id
    strong = [
        (own_value, other_value)
        for own_value, other_value in (
            (_own_host(own.host), _own_host(other.host)),
            (own.phone, other.phone),
        )
        if own_value and other_value
    ]
    if any(own_value == other_value for own_value, other_value in strong):
        return True
    if own.address and other.address:
        if own.address != other.address:
            return False
        return not strong or names_agree(own.name, other.name)
    return False if strong else None


def same_business(own: Identity, other: Identity) -> bool:
    by_features = compare_features(own, other)
    return names_agree(own.name, other.name) if by_features is None else by_features
