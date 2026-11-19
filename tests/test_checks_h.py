import pytest

from radarauditor.collectors.snapshot import DirectoryEntry, DirectoryHit, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import address_key, phone_key
from radarauditor.scoring.checks.h_verzeichnisse import run

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "h1": {},
    "h2": {},
}


def _hit(directory: str, found: bool = True, **fields: str | None) -> DirectoryHit:
    values: dict[str, str | None] = {
        "name": "Dachdeckerei Muster GmbH",
        "address": "Hauptstraße 5, 48143 Münster",
        "phone": "0251 123456",
    }
    values.update(fields)
    return DirectoryHit(
        directory=directory,
        found=found,
        listing_url=None,
        name=values["name"] if found else None,
        address=values["address"] if found else None,
        phone=values["phone"] if found else None,
    )


def _with_hits(raw: RawAudit, hits: list[DirectoryHit]) -> RawAudit:
    return raw.model_copy(update={"directories": hits})


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, THRESHOLDS[check_id])[0] is (
        CheckOutcome.NOT_MEASURABLE
    )


def test_normalisation_keys() -> None:
    assert phone_key("+49 (251) 12 34-56") == phone_key("0251/123456") == "0251123456"
    assert phone_key("0049 251 123456") == "0251123456"
    assert phone_key("") is None
    assert address_key("Hauptstr. 5, 48143 Münster") == address_key(
        "Hauptstraße 5 48143 Münster, Deutschland"
    )
    assert address_key("Ringweg 1, Berlin") == "ringweg1|"
    assert address_key(None) is None


def test_h1_found_with_different_phone_is_partial(raw_audit_full: RawAudit) -> None:
    raw = _with_hits(raw_audit_full, [_hit("Apple Business Connect", phone="0251 999")])
    outcome, measured = run("h1", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["listings"]["Apple Business Connect"]["mismatches"] == ["phone"]


def _empty_answer(
    directory: str,
    delivered: int | None,
    search_text: str | None = "Dachdeckerei Muster GmbH, Hauptstraße 5, 48143 Münster",
    entries: list[DirectoryEntry] | None = None,
) -> DirectoryHit:
    return DirectoryHit(
        directory=directory,
        found=False,
        listing_url=None,
        name=None,
        address=None,
        phone=None,
        delivered=delivered,
        search_text=search_text,
        entries=entries,
    )


def test_h1_not_found_fails(raw_audit_full: RawAudit) -> None:
    raw = _with_hits(raw_audit_full, [_empty_answer("Apple Business Connect", 3)])
    assert run("h1", raw, {})[0] is CheckOutcome.FAILED


@pytest.mark.parametrize(
    ("check_id", "directory"), [("h1", "Apple Business Connect"), ("h2", "Bing Places")]
)
@pytest.mark.parametrize(
    ("delivered", "search_text"),
    [(None, "Dachdeckerei Muster, Münster"), (5, None), (5, ""), (5, "   "), (None, None)],
)
def test_not_found_without_the_search_text_or_the_count_of_entries_is_not_measured(
    raw_audit_full: RawAudit,
    check_id: str,
    directory: str,
    delivered: int | None,
    search_text: str | None,
) -> None:
    raw = _with_hits(raw_audit_full, [_empty_answer(directory, delivered, search_text)])
    assert run(check_id, raw, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "directory_search_not_documented"},
    )


def test_a_found_listing_needs_no_documented_search(raw_audit_full: RawAudit) -> None:
    raw = _with_hits(raw_audit_full, [_hit("Bing Places")])
    assert run("h2", raw, {})[0] is CheckOutcome.PASSED


def test_the_delivered_entries_are_kept_for_the_control(raw_audit_full: RawAudit) -> None:
    entries = [
        DirectoryEntry(name="Anders", address="Weg 1, Hamburg", phone="040 123"),
        DirectoryEntry(name="Noch anders"),
    ]
    answer = _empty_answer("Bing Places", 2, entries=entries)
    measured = run("h2", _with_hits(raw_audit_full, [answer]), {})[1]
    assert measured["listings"]["Bing Places"]["entries"] == [
        entry.model_dump() for entry in entries
    ]


@pytest.mark.parametrize(
    ("check_id", "directory"), [("h1", "Apple Business Connect"), ("h2", "Bing Places")]
)
def test_a_service_that_delivered_no_entry_at_all_is_not_measured(
    raw_audit_full: RawAudit, check_id: str, directory: str
) -> None:
    raw = _with_hits(raw_audit_full, [_empty_answer(directory, 0)])
    outcome, measured = run(check_id, raw, {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured["reason"] == "service_delivered_nothing"


@pytest.mark.parametrize(
    ("check_id", "directory"), [("h1", "Apple Business Connect"), ("h2", "Bing Places")]
)
def test_entries_were_delivered_but_none_is_the_business_means_no_listing(
    raw_audit_full: RawAudit, check_id: str, directory: str
) -> None:
    raw = _with_hits(raw_audit_full, [_empty_answer(directory, 5)])
    outcome, measured = run(check_id, raw, {})
    assert outcome is CheckOutcome.FAILED
    details = measured["listings"][directory]
    assert details["delivered"] == 5
    assert details["search_text"] == "Dachdeckerei Muster GmbH, Hauptstraße 5, 48143 Münster"


def test_h2_found_with_different_phone_is_partial(raw_audit_full: RawAudit) -> None:
    raw = _with_hits(raw_audit_full, [_hit("Bing Places", phone="0251 999")])
    outcome, measured = run("h2", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["listings"]["Bing Places"]["mismatches"] == ["phone"]


def test_h2_not_found_fails(raw_audit_full: RawAudit) -> None:
    raw = _with_hits(raw_audit_full, [_empty_answer("Bing Places", 3)])
    assert run("h2", raw, {})[0] is CheckOutcome.FAILED


def test_listing_compared_with_footer_without_place(raw_audit_full: RawAudit) -> None:
    raw = _with_hits(raw_audit_full, [_hit("Bing Places", address="Ringweg 1, 10115 Berlin")])
    raw = raw.model_copy(update={"place": None})
    outcome, measured = run("h2", raw, {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["listings"]["Bing Places"]["mismatches"] == ["address"]
