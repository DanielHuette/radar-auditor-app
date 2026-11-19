import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from radarauditor.errors import ScoringError

logger = logging.getLogger(__name__)

TOTAL_WEIGHT = Decimal(100)
AGENT_SOURCE = "agent"
AGENT_PENDING_REASON = "agent_judgement"
CHECK_ID_PATTERN = re.compile(r"^[a-z]\d+$")
PAID_MEASUREMENTS = frozenset({"paid", "ads_google", "ads_meta"})
ACCESS_MEASUREMENT = "access"
KNOWN_MEASUREMENTS = frozenset(
    {
        "crawler",
        "pagespeed",
        "places",
        "serp",
        "directories",
        "assistants",
        "ads_google",
        "ads_meta",
        ACCESS_MEASUREMENT,
        "paid",
        AGENT_SOURCE,
    }
)
PROVIDER_SECTIONS = {
    "google": "google",
    "bing": "bing",
    "apple": "apple",
    "openai": "openai",
    "anthropic": "anthropic",
    "google_ads": "ads",
    "meta_ads": "ads",
}
KIND_CRITERION = "kriterium"
KIND_INFO = "info"
SOURCE_LIST = frozenset(
    {
        "https://developers.google.com/search/docs/essentials",
        "https://developers.google.com/search/docs/essentials/technical",
        "https://developers.google.com/search/docs/appearance/page-experience",
        "https://developers.google.com/search/docs/appearance/ranking-systems-guide",
        "https://developers.google.com/search/docs/crawling-indexing/links-crawlable",
        "https://developers.google.com/search/docs/appearance/ai-features",
        "https://developers.google.com/crawling/docs/crawlers-fetchers/google-common-crawlers",
        "https://developers.google.com/search/docs/essentials/spam-policies",
        "https://support.google.com/business/answer/7091?hl=de",
        "https://www.bing.com/webmasters/help/webmaster-guidelines-30fba23a",
        "https://developers.openai.com/api/docs/bots",
        "https://support.claude.com/en/articles/8896518",
        "https://ahrefs.com/rank-tracker",
        "https://help.brightlocal.com/hc/en-us/articles/37024870670738-What-is-Local-Search-Grid-New-Platform",
        "https://www.localfalcon.com/blog/how-local-falcon-measures-your-local-rankings-and-visibility-across-ai-platforms-and-google-maps",
    }
)


class CheckDefinition(BaseModel):
    id: str
    area: str
    name: str
    why: str
    provider: str
    kind: Literal["kriterium", "ergebnis", "info"]
    weight: Decimal | None = None
    source: str = ""
    measurement: str
    requires_paid: bool
    requires_access: bool
    thresholds: dict[str, float | int | str]
    customer_question: str
    customer_impact: str

    @property
    def counts_in_score(self) -> bool:
        return self.kind != KIND_INFO


class AreaDefinition(BaseModel):
    id: str
    name: str
    weight: Decimal
    checks: list[CheckDefinition]


class Catalogue(BaseModel):
    areas: list[AreaDefinition]


def load_catalogue(path: Path) -> Catalogue:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ScoringError(f"Cannot read check catalogue {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ScoringError(f"Check catalogue {path} is not valid YAML: {error}") from error
    catalogue = catalogue_from_document(document, str(path))
    logger.debug("Loaded check catalogue %s with %d areas", path, len(catalogue.areas))
    return catalogue


def catalogue_from_document(document: object, origin: str) -> Catalogue:
    if not isinstance(document, dict) or not isinstance(document.get("areas"), list):
        raise ScoringError(f"Check catalogue {origin} has no list of areas")
    try:
        catalogue = Catalogue.model_validate({"areas": _with_area_ids(document["areas"])})
    except ValidationError as error:
        raise ScoringError(f"Check catalogue {origin} is malformed: {error}") from error
    validate_catalogue(catalogue)
    return catalogue


def validate_catalogue(catalogue: Catalogue) -> None:
    weights = sum(area.weight for area in catalogue.areas)
    if weights != TOTAL_WEIGHT:
        raise ScoringError(f"Area weights sum to {weights}, expected {TOTAL_WEIGHT}")
    area_ids = [area.id for area in catalogue.areas]
    if len(set(area_ids)) != len(area_ids):
        raise ScoringError("Area ids are not unique")
    seen: set[str] = set()
    for area in catalogue.areas:
        if area.weight < 0:
            raise ScoringError(f"Area {area.id} has a negative weight")
        _validate_check_weights(area)
        for check in area.checks:
            _validate_check(check, area.id, seen)
            seen.add(check.id)


def _validate_check_weights(area: AreaDefinition) -> None:
    weights = [check.weight for check in area.checks]
    if all(weight is None for weight in weights):
        return
    if any(weight is None for weight in weights):
        raise ScoringError(f"Area {area.id}: either every check has a weight or none")
    given = [weight for weight in weights if weight is not None]
    if any(weight < 0 for weight in given):
        raise ScoringError(f"Area {area.id} has a check with a negative weight")
    if sum(given) != area.weight:
        raise ScoringError(
            f"Area {area.id}: check weights sum to {sum(given)}, area weight is {area.weight}"
        )


def find_check(catalogue: Catalogue, check_id: str) -> CheckDefinition:
    for area in catalogue.areas:
        for check in area.checks:
            if check.id == check_id:
                return check
    raise ScoringError(f"Check {check_id} is not in the catalogue")


def checks_for_context(
    catalogue: Catalogue, paid_enabled: bool, with_access: bool
) -> list[CheckDefinition]:
    return [
        check
        for area in catalogue.areas
        for check in area.checks
        if (paid_enabled or not check.requires_paid) and (with_access or not check.requires_access)
    ]


def _with_area_ids(areas: list[object]) -> list[object]:
    enriched: list[object] = []
    for area in areas:
        if not isinstance(area, dict) or not isinstance(area.get("checks"), list):
            raise ScoringError("Every area needs an id and a list of checks")
        checks = [
            {**check, "area": area.get("id")} if isinstance(check, dict) else check
            for check in area["checks"]
        ]
        enriched.append({**area, "checks": checks})
    return enriched


def _validate_check(check: CheckDefinition, area_id: str, seen: set[str]) -> None:
    if not CHECK_ID_PATTERN.fullmatch(check.id):
        raise ScoringError(f"Check id {check.id!r} does not match {CHECK_ID_PATTERN.pattern}")
    if check.id in seen:
        raise ScoringError(f"Check id {check.id} is defined twice")
    section = PROVIDER_SECTIONS.get(check.provider)
    if section is None:
        raise ScoringError(f"Check {check.id} has unknown provider {check.provider!r}")
    if section != area_id:
        raise ScoringError(
            f"Check {check.id} of provider {check.provider} is filed under section {area_id}"
        )
    if check.source not in SOURCE_LIST and (check.kind == KIND_CRITERION or check.source):
        raise ScoringError(
            f"Check {check.id} needs an address from the source list as source, "
            f"got {check.source!r}"
        )
    if check.measurement not in KNOWN_MEASUREMENTS:
        raise ScoringError(f"Check {check.id} has unknown measurement {check.measurement!r}")
    if (check.measurement in PAID_MEASUREMENTS) != check.requires_paid:
        raise ScoringError(f"Check {check.id}: measurement and requires_paid disagree")
    if (check.measurement == ACCESS_MEASUREMENT) != check.requires_access:
        raise ScoringError(f"Check {check.id}: measurement and requires_access disagree")
