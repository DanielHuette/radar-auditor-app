import re
from pathlib import Path

from radarauditor.scoring.catalog import load_catalogue

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "030_product_radar_areas_sections.sql"
OLD_LETTERS = set("abcdefghijklmno")
DROPPED_LETTERS = {"l", "n", "o"}


def _mapping() -> dict[str, set[str]]:
    text = MIGRATION.read_text(encoding="utf-8")
    pairs = re.findall(r"\('([a-z])',\s*'([a-z]+)'\)", text)
    mapping: dict[str, set[str]] = {}
    for letter, section in pairs:
        mapping.setdefault(letter, set()).add(section)
    return mapping


def test_every_section_the_migration_writes_is_a_section_of_the_catalogue() -> None:
    catalogue = load_catalogue(ROOT / "config" / "pruefkatalog.yaml")
    sections = {area.id for area in catalogue.areas}
    written = {section for targets in _mapping().values() for section in targets}
    assert written <= sections


def test_every_old_letter_is_mapped_or_named_as_dropped() -> None:
    mapped = set(_mapping())
    assert mapped | DROPPED_LETTERS == OLD_LETTERS
    assert not mapped & DROPPED_LETTERS


def test_the_migration_keeps_sections_that_are_already_new_and_only_touches_old_letters() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert "where entry in ('google', 'bing', 'apple', 'openai', 'anthropic')" in text
    assert "update crm.products" in text
    assert "delete" not in text.lower()
