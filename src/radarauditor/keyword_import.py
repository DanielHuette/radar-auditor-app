import codecs
import csv
import re
from dataclasses import dataclass
from pathlib import Path

from radarauditor.collectors.patterns import collapse_whitespace
from radarauditor.db.models import Customer, KeywordScope
from radarauditor.db.repositories import Repositories

PHRASE_COLUMN = "keyword"
VOLUME_COLUMN_MARKS = (("avg", "monthly", "searches"), ("suchanfragen", "monat"))
VOLUME = re.compile(r"(\d[\d.,]*)\s*(k|m|tsd|mio)?", re.IGNORECASE)
SHORTENED_FRACTION = re.compile(r"(\d+)[.,](\d{1,2})")
MULTIPLIERS = {"k": 1_000, "tsd": 1_000, "m": 1_000_000, "mio": 1_000_000}
PLACE_PREPOSITION = "in"
UTF16_MARKS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


@dataclass(frozen=True)
class PlannedKeyword:
    phrase: str
    volume: int | None


@dataclass(frozen=True)
class ImportCount:
    added: int
    already_stored: int
    with_volume: int


def read_planner_export(path: Path) -> list[PlannedKeyword]:
    raw = path.read_bytes()
    text = raw.decode("utf-16" if raw.startswith(UTF16_MARKS) else "utf-8-sig")
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if _is_header(line)), None)
    if start is None:
        raise ValueError(f"{path}: no table with a column named Keyword")
    delimiter = "\t" if "\t" in lines[start] else ","
    rows = list(csv.reader(lines[start:], delimiter=delimiter))
    header = [cell.strip().casefold() for cell in rows[0]]
    phrase_at = header.index(PHRASE_COLUMN)
    volume_at = next(
        (
            index
            for index, name in enumerate(header)
            if any(all(mark in name for mark in marks) for marks in VOLUME_COLUMN_MARKS)
        ),
        None,
    )
    planned: dict[str, PlannedKeyword] = {}
    for row in rows[1:]:
        phrase = collapse_whitespace(row[phrase_at]) if len(row) > phrase_at else ""
        if not phrase:
            continue
        volume = (
            volume_of(row[volume_at]) if volume_at is not None and len(row) > volume_at else None
        )
        planned.setdefault(phrase.casefold(), PlannedKeyword(phrase, volume))
    return list(planned.values())


def _is_header(line: str) -> bool:
    return line.replace(",", "\t").split("\t")[0].strip().strip('"').casefold() == PHRASE_COLUMN


def volume_of(text: str) -> int | None:
    match = VOLUME.match(text.strip())
    if match is None:
        return None
    multiplier = MULTIPLIERS.get((match.group(2) or "").casefold(), 1)
    fraction = SHORTENED_FRACTION.fullmatch(match.group(1)) if multiplier > 1 else None
    if fraction is not None:
        tenths = fraction.group(2).ljust(2, "0")
        return (int(fraction.group(1)) * 100 + int(tenths)) * multiplier // 100
    return int(re.sub(r"[.,]", "", match.group(1))) * multiplier


def without_city(planned: list[PlannedKeyword], city: str) -> list[PlannedKeyword]:
    words = city.casefold().split()
    kept: dict[str, PlannedKeyword] = {}
    for keyword in planned:
        phrase = keyword.phrase.split()
        folded = [word.casefold() for word in phrase]
        if words and len(phrase) > len(words):
            if folded[-len(words) :] == words:
                phrase = phrase[: -len(words)]
                if len(phrase) > 1 and phrase[-1].casefold() == PLACE_PREPOSITION:
                    phrase = phrase[:-1]
            elif folded[: len(words)] == words:
                phrase = phrase[len(words) :]
        cleaned = " ".join(phrase)
        kept.setdefault(cleaned.casefold(), PlannedKeyword(cleaned, keyword.volume))
    return list(kept.values())


def import_keywords(
    repositories: Repositories,
    customer: Customer,
    planned: list[PlannedKeyword],
    scope: KeywordScope = KeywordScope.LOCAL,
) -> ImportCount:
    stored = {keyword.phrase.casefold(): keyword for keyword in repositories.keywords(customer.id)}
    if scope is KeywordScope.LOCAL:
        planned = without_city(planned, customer.city)
    new = [keyword for keyword in planned if keyword.phrase.casefold() not in stored]
    added = repositories.add_keywords(
        customer.id,
        [keyword.phrase for keyword in new],
        customer.city,
        first_primary=not any(keyword.is_primary for keyword in stored.values()),
        scope=scope,
    )
    stored.update({keyword.phrase.casefold(): keyword for keyword in added})
    with_volume = 0
    for keyword in planned:
        if keyword.volume is not None:
            repositories.update_keyword_volume(stored[keyword.phrase.casefold()].id, keyword.volume)
            with_volume += 1
    return ImportCount(len(added), len(planned) - len(new), with_volume)
