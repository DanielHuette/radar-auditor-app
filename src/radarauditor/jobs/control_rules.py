import importlib
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from radarauditor.errors import ScoringError
from radarauditor.scoring.catalog import Catalogue

BLOCKS_KEY = "blocks"
CHECKS_KEY = "checks"
CHECK_MODULES = "radarauditor.scoring.checks"
PERCENT = 100
PLACEHOLDER = re.compile(r"\{(block|t|c):([A-Za-z0-9_.]+)(?:\|([a-z]+))?\}")
NO_DATA_BLOCK = "no_data"
NOT_MEASURED = "nicht gemessen"
GERMAN_QUOTES = "„{}“"

type Text = Mapping[str, str]


def read_rules(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ScoringError(f"Control rules {path} cannot be read: {error}") from error
    if not isinstance(document, dict):
        raise ScoringError(f"Control rules {path} must hold the sections {BLOCKS_KEY} and checks")
    return _texts(document, BLOCKS_KEY, path), _texts(document, CHECKS_KEY, path)


def _texts(document: dict[object, object], key: str, path: Path) -> dict[str, str]:
    section = document.get(key)
    if not isinstance(section, dict) or not all(
        isinstance(name, str) and isinstance(text, str) for name, text in section.items()
    ):
        raise ScoringError(f"Control rules {path}: section {key} must map names to texts")
    return {name: " ".join(text.split()) for name, text in section.items()}


def load_rules(path: Path, catalogue: Catalogue) -> dict[str, str]:
    blocks, checks = read_rules(path)
    thresholds = {check.id: check.thresholds for area in catalogue.areas for check in area.checks}
    rendered = {
        check_id: render(text, blocks, thresholds.get(check_id, {}), check_id)
        for check_id, text in checks.items()
    }
    return {check_id: _with_no_data(text, blocks) for check_id, text in rendered.items()}


def _with_no_data(text: str, blocks: Text) -> str:
    if NOT_MEASURED in text:
        return text
    return f"{text} {blocks[NO_DATA_BLOCK]}"


def render(
    text: str, blocks: Text, thresholds: Mapping[str, float | int | str], origin: str
) -> str:
    expanded = PLACEHOLDER.sub(lambda match: _block(match, blocks, origin), text)
    return PLACEHOLDER.sub(lambda match: _value(match, thresholds, origin), expanded)


def _block(match: re.Match[str], blocks: Text, origin: str) -> str:
    if match.group(1) != "block":
        return match.group(0)
    block = blocks.get(match.group(2))
    if block is None:
        raise ScoringError(f"Control rule {origin}: unknown block {match.group(2)!r}")
    return block


def _value(match: re.Match[str], thresholds: Mapping[str, float | int | str], origin: str) -> str:
    kind, name, style = match.group(1), match.group(2), match.group(3)
    if kind == "t":
        if name not in thresholds:
            raise ScoringError(f"Control rule {origin}: the catalogue has no threshold {name!r}")
        return _shown(thresholds[name], style, origin)
    return _shown(constant(name, origin), style, origin)


def constant(name: str, origin: str = "") -> object:
    module_name, _, attribute = name.partition(".")
    try:
        module = importlib.import_module(f"{CHECK_MODULES}.{module_name}")
        return getattr(module, attribute)
    except (ImportError, AttributeError) as error:
        raise ScoringError(f"Control rule {origin}: no constant {name!r} in the checks") from error


def _shown(value: object, style: str | None, origin: str) -> str:
    if style == "list" and isinstance(value, tuple | list | frozenset):
        return ", ".join(GERMAN_QUOTES.format(item) for item in value)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ScoringError(f"Control rule {origin}: {value!r} cannot be shown as a number")
    if style == "pct" and isinstance(value, int | float):
        return _number(value * PERCENT)
    if style not in (None, "pct"):
        raise ScoringError(f"Control rule {origin}: unknown style {style!r}")
    return _number(value) if isinstance(value, int | float) else value


def _number(value: float) -> str:
    rounded = round(value, 6)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.6f}".rstrip("0").replace(".", ",")
