import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from radarauditor.errors import ScoringError
from radarauditor.scoring.catalog import AGENT_SOURCE, Catalogue

logger = logging.getLogger(__name__)

ASSISTANT_PREFIX = "assistant:"
ASSISTANT_GROUP = "assistants"
SOURCE_ROUTES = {
    "crawler": "website",
    "pagespeed": "pagespeed",
    "places": "google_maps",
    "serp": "google_search",
    "directories": "directories",
    "assistants": ASSISTANT_GROUP,
    "paid": "dataforseo",
    "access": "access",
    "ads_google": "google_ads",
    "ads_meta": "meta_ads",
    AGENT_SOURCE: "website",
}
ROUTE_LABELS = {
    "website": "Website",
    "pagespeed": "PageSpeed",
    "google_maps": "Google Maps",
    "google_search": "Google-Suche",
    "bing": "Bing-Suche",
    "google_images": "Google-Bildersuche",
    "directories": "Verzeichnisse",
    "dataforseo": "DataForSEO",
    "access": "Kundenzugang",
    "assistants": "KI-Assistenten",
    "review_portals": "Bewertungsportale",
    "apple_maps": "Apple Karten",
    "bing_maps": "Bing Karten",
    "industry_directories": "Branchenverzeichnisse",
    "google_ads": "Google-Anzeigen",
    "meta_ads": "Meta-Anzeigen",
    "wikidata": "Wikidata",
    "assistant:chatgpt": "ChatGPT",
    "assistant:google_ai": "Googles KI-Modus",
    "assistant:claude": "Claude",
}


def route_label(route: str) -> str:
    return ROUTE_LABELS.get(route, route)


class _Entry(BaseModel):
    number: int = Field(ge=1)
    stage: int = Field(ge=0, le=4)
    route: str | None = None


class Supplier(BaseModel):
    model_config = ConfigDict(extra="allow")

    supplier: str
    endpoint: str
    key: str | None
    built: bool


class MeasurementRoutes(BaseModel):
    label: str
    routes: list[Supplier] = Field(default_factory=list)
    variants: dict[str, list[Supplier]] = Field(default_factory=dict)

    def suppliers(self) -> list[Supplier]:
        return [*self.routes, *(item for group in self.variants.values() for item in group)]


class _Document(BaseModel):
    suppliers: dict[str, str]
    checks: dict[str, _Entry]
    measurements: dict[str, MeasurementRoutes]


def _read_document(path: Path) -> _Document:
    try:
        document = _Document.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except OSError as error:
        raise ScoringError(f"Messwege file {path}: cannot read it ({error})") from error
    except (yaml.YAMLError, ValidationError) as error:
        raise ScoringError(f"Messwege file {path} is malformed: {error}") from error
    for name, measurement in document.measurements.items():
        for item in measurement.suppliers():
            if item.supplier not in document.suppliers:
                raise ScoringError(
                    f"Messwege file {path}: measurement {name} names unknown supplier "
                    f"{item.supplier}"
                )
    return document


def load_measurements(path: Path) -> dict[str, MeasurementRoutes]:
    return _read_document(path).measurements


@cache
def supplier_labels(path: Path) -> dict[str, str]:
    return _read_document(path).suppliers


@dataclass(frozen=True)
class RouteCheck:
    number: int
    id: str
    name: str
    stage: int
    route: str


class RouteMap:
    def __init__(self, checks: list[RouteCheck]) -> None:
        self._checks = sorted(checks, key=lambda check: check.number)
        self._by_id = {check.id: check for check in self._checks}

    def entry(self, check_id: str) -> RouteCheck:
        try:
            return self._by_id[check_id]
        except KeyError:
            raise ScoringError(f"Check {check_id} has no route") from None

    def number_of(self, check_id: str) -> int:
        return self.entry(check_id).number

    def route_of(self, check_id: str) -> str:
        return self.entry(check_id).route

    def checks_of(self, route: str) -> list[RouteCheck]:
        group = ASSISTANT_GROUP if route.startswith(ASSISTANT_PREFIX) else route
        return [check for check in self._checks if check.route == group]

    def stage_of(self, route: str) -> int:
        checks = self.checks_of(route)
        if not checks:
            raise ScoringError(f"Route {route} serves no check")
        return min(check.stage for check in checks)


def load_route_map(path: Path, catalogue: Catalogue) -> RouteMap:
    document = _read_document(path)
    checks: list[RouteCheck] = []
    numbers: set[int] = set()
    for area in catalogue.areas:
        for definition in area.checks:
            entry = document.checks.get(definition.id)
            if entry is None:
                raise ScoringError(f"Messwege file {path} has no entry for check {definition.id}")
            if entry.number in numbers:
                raise ScoringError(f"Messwege file {path} uses number {entry.number} twice")
            numbers.add(entry.number)
            if definition.measurement not in document.measurements:
                raise ScoringError(
                    f"Messwege file {path} has no measurement {definition.measurement} "
                    f"for check {definition.id}"
                )
            route = entry.route or SOURCE_ROUTES[definition.measurement]
            checks.append(
                RouteCheck(entry.number, definition.id, definition.name, entry.stage, route)
            )
    logger.debug("Loaded %d routes of checks from %s", len(checks), path)
    return RouteMap(checks)
