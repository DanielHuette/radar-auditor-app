import pytest

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.errors import ScoringError
from radarauditor.scoring.checks.i_links import run

PAID = [
    "i1",
]


@pytest.mark.parametrize("check_id", PAID)
def test_paid_checks_not_measurable_without_paid_data(
    raw_audit_full: RawAudit, check_id: str
) -> None:
    raw = raw_audit_full.model_copy(update={"paid": None})
    assert run(check_id, raw, {}) == (CheckOutcome.NOT_MEASURABLE, {"reason": "no_paid_data"})


@pytest.mark.parametrize("check_id", PAID)
def test_paid_checks_pass_for_full_audit(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, {})[0] is CheckOutcome.PASSED


def _with_links_of(raw: RawAudit, **updates: object) -> RawAudit:
    assert raw.paid is not None and raw.paid.customer_domain is not None
    own = raw.paid.backlinks[raw.paid.customer_domain].model_copy(update=updates)
    backlinks = {**raw.paid.backlinks, own.domain: own}
    return raw.model_copy(update={"paid": raw.paid.model_copy(update={"backlinks": backlinks})})


@pytest.mark.parametrize(
    ("domains", "expected"),
    [(47, CheckOutcome.PASSED), (46, CheckOutcome.PASSED), (45, CheckOutcome.FAILED)],
)
def test_i1_compares_with_the_median_of_the_competitors(
    raw_audit_full: RawAudit, domains: int, expected: CheckOutcome
) -> None:
    outcome, measured = run("i1", _with_links_of(raw_audit_full, referring_domains=domains), {})
    assert outcome is expected
    assert measured["competitor_median"] == 46.0
    assert measured["best_competitor"] == "dach-schmidt.de"


def test_i1_needs_own_and_competitor_profiles(raw_audit_full: RawAudit) -> None:
    assert raw_audit_full.paid is not None
    no_own = raw_audit_full.paid.model_copy(update={"customer_domain": None})
    assert run("i1", raw_audit_full.model_copy(update={"paid": no_own}), {})[1] == {
        "reason": "no_backlink_data"
    }
    alone = raw_audit_full.paid.model_copy(
        update={
            "backlinks": {
                key: value
                for key, value in raw_audit_full.paid.backlinks.items()
                if key == raw_audit_full.paid.customer_domain
            }
        }
    )
    assert run("i1", raw_audit_full.model_copy(update={"paid": alone}), {})[1] == {
        "reason": "no_competitor_backlinks"
    }


@pytest.mark.parametrize("check_id", PAID)
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, {})[0] is CheckOutcome.NOT_MEASURABLE


def test_unknown_check_raises(raw_audit_full: RawAudit) -> None:
    with pytest.raises(ScoringError):
        run("i9", raw_audit_full, {})
