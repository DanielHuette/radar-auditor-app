from pathlib import Path

import pytest
import yaml

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.feedback.events import CODE_REASONS
from radarauditor.scoring.catalog import CheckDefinition, load_catalogue
from radarauditor.scoring.evaluator import run_check

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = ROOT / "config" / "pruefkatalog.yaml"
LABELS_PATH = ROOT / "templates" / "report" / "labels.yaml"
RECORDING_CANDIDATES = (
    ROOT / "work" / "belege_96b659f8" / "raw.json",
    Path("/out/belege_96b659f8/raw.json"),
)
MEASURED_OUTCOMES = {
    CheckOutcome.PASSED,
    CheckOutcome.PARTIAL,
    CheckOutcome.FAILED,
}


def _recording() -> Path:
    for candidate in RECORDING_CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.skip("recorded audit is not available")


def _known_reasons() -> set[str]:
    labels = yaml.safe_load(LABELS_PATH.read_text(encoding="utf-8"))
    return set(labels["reasons"]) | set(CODE_REASONS)


def _all_checks() -> list[CheckDefinition]:
    catalogue = load_catalogue(CATALOGUE_PATH)
    return [check for area in catalogue.areas for check in area.checks]


@pytest.fixture(scope="module")
def recorded() -> RawAudit:
    return RawAudit.model_validate_json(_recording().read_text(encoding="utf-8"))


@pytest.mark.parametrize("check", _all_checks(), ids=lambda check: check.id)
def test_every_check_handles_the_recorded_audit(check: CheckDefinition, recorded: RawAudit) -> None:
    outcome, measured = run_check(check, recorded)

    if outcome in MEASURED_OUTCOMES:
        assert measured
        assert "reason" not in measured
    else:
        assert measured["reason"] in _known_reasons()


def test_the_recorded_audit_lacks_the_new_crawl_data(recorded: RawAudit) -> None:
    assert recorded.website is not None
    assert recorded.website.agent_fetches is None
    assert recorded.website.variant_probes is None
    assert recorded.website.sitemap_probes is None
