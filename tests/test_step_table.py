import json

from conftest import REPORT_ROOT

from radarauditor.collectors.validation import steps_of_check
from radarauditor.scoring.catalog import Catalogue


def test_the_step_table_of_the_app_matches_the_steps_of_the_collector(
    catalogue: Catalogue,
) -> None:
    document = json.loads((REPORT_ROOT / "config" / "schritte.json").read_text(encoding="utf-8"))

    expected = {
        check.id: list(steps_of_check(check.id, check.measurement))
        for area in catalogue.areas
        for check in area.checks
    }

    assert document == expected
