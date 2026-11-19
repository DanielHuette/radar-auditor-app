from datetime import UTC, datetime
from uuid import UUID

import pytest

from radarauditor.collectors.snapshot import (
    GridPoint,
    MapRanking,
    PositionChange,
    RankingHistory,
    RawAudit,
    SerpResult,
)
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.j_rankings import run

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "j1": {"top": 3, "good": 10, "visible": 20},
    "j2": {"top": 3},
    "j4": {},
    "j6": {},
    "j7": {"good": 10},
    "j8": {},
}
SITE = "https://www.dachdeckerei-muster.de"


def _serp(
    phrase: str,
    organic: list[tuple[int, str]],
    local_pack: list[tuple[int, str]],
    **extra: object,
) -> SerpResult:
    return SerpResult.model_validate(
        {
            "phrase": phrase,
            "organic": organic,
            "local_pack": local_pack,
            "ai_overview_present": False,
            "ai_overview_mentions": [],
            "people_also_ask": [],
            "measured_at": "2026-10-01T12:00:00Z",
            **extra,
        }
    )


def _with_serps(raw: RawAudit, serps: list[SerpResult]) -> RawAudit:
    return raw.model_copy(update={"serps": serps})


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", [*THRESHOLDS, "j3", "j5", "j9"])
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, THRESHOLDS.get(check_id, {}))[0] is (
        CheckOutcome.NOT_MEASURABLE
    )


@pytest.mark.parametrize(
    ("check_id", "reason"),
    [
        ("j3", "no_grid"),
        ("j9", "needs_history"),
    ],
)
def test_uncollected_checks_report_reason(
    raw_audit_full: RawAudit, check_id: str, reason: str
) -> None:
    assert run(check_id, raw_audit_full, {})[1] == {"reason": reason}


def _with_counts(raw: RawAudit, counts: dict[str, int]) -> RawAudit:
    assert raw.paid is not None
    return raw.model_copy(update={"paid": raw.paid.model_copy(update={"ranked_keywords": counts})})


@pytest.mark.parametrize(
    ("own", "expected"),
    [(120, CheckOutcome.PASSED), (60, CheckOutcome.PARTIAL), (59, CheckOutcome.FAILED)],
)
def test_j5_compares_ranked_keywords_with_best_competitor(
    raw_audit_full: RawAudit, own: int, expected: CheckOutcome
) -> None:
    raw = _with_counts(raw_audit_full, {"dachdeckerei-muster.de": own, "dach-schmidt.de": 150})
    outcome, measured = run("j5", raw, {})
    assert outcome is expected
    assert measured["best_competitor"] == "dach-schmidt.de"
    assert measured["ranked_keywords"] == own


def test_j5_needs_paid_data_and_competitors(raw_audit_full: RawAudit) -> None:
    assert run("j5", raw_audit_full, {})[0] is CheckOutcome.PASSED
    without = raw_audit_full.model_copy(update={"paid": None})
    assert run("j5", without, {})[1] == {"reason": "no_paid_data"}
    assert run("j5", _with_counts(raw_audit_full, {}), {})[1] == {"reason": "no_ranked_keywords"}
    alone = _with_counts(raw_audit_full, {"dachdeckerei-muster.de": 3})
    assert run("j5", alone, {})[1] == {"reason": "no_competitor_keywords"}
    nobody = _with_counts(raw_audit_full, {"dachdeckerei-muster.de": 0, "dach-schmidt.de": 0})
    assert run("j5", nobody, {})[0] is CheckOutcome.PASSED


def _with_grid(raw: RawAudit, positions: list[int | None]) -> RawAudit:
    grid = [
        GridPoint(key=f"r1c{index}", latitude=51.9, longitude=7.6, position=position)
        for index, position in enumerate(positions, start=1)
    ]
    return raw.model_copy(update={"grid": grid})


@pytest.mark.parametrize(
    "positions",
    [
        [1, 2, 3, 1, 2, 3, 3, 4, None, None],
        [1, 2, 3, 4, 4, 4, 4, 4, None, None],
        [None] * 10,
    ],
)
def test_j3_gives_the_measured_value_without_a_verdict(
    raw_audit_full: RawAudit, positions: list[int | None]
) -> None:
    outcome, measured = run("j3", _with_grid(raw_audit_full, positions), {"grid": 5})
    assert outcome is CheckOutcome.PASSED
    assert measured["points_total"] == 10
    assert measured["points_in_top"] == sum(
        1 for position in positions if position is not None and position <= 3
    )
    assert measured["competitor_values"] is None
    assert measured["positions"] == {
        f"r1c{index}": position for index, position in enumerate(positions, start=1)
    }


def test_j3_counts_the_top_positions_from_the_catalogue(raw_audit_full: RawAudit) -> None:
    raw = _with_grid(raw_audit_full, [1, 5, None, None])
    outcome, measured = run("j3", raw, {"top": 5})
    assert outcome is CheckOutcome.PASSED
    assert (measured["points_in_top"], measured["share_in_top"]) == (2, 0.5)


def test_j1_positions_and_counts(raw_audit_full: RawAudit) -> None:
    serps = [
        _serp("Dachdecker Münster", [(1, "https://a.de"), (2, f"{SITE}/")], []),
        _serp("Dachsanierung Münster", [(15, f"{SITE}/dachsanierung/"), (18, f"{SITE}/")], []),
        _serp("Dachfenster Münster", [(1, "https://b.de")], []),
        _serp("Dachdeckerei Muster", [(1, f"{SITE}/")], []),
    ]
    outcome, measured = run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])
    assert outcome is CheckOutcome.PARTIAL
    assert measured["positions"] == {
        "Dachdecker Münster": 2,
        "Dachsanierung Münster": 15,
        "Dachfenster Münster": None,
    }
    assert (measured["in_top"], measured["in_good"], measured["in_visible"]) == (1, 1, 2)
    assert measured["phrases_total"] == 3


def test_j1_subdomain_counts_and_nothing_visible_fails(raw_audit_full: RawAudit) -> None:
    serps = [_serp("Dachdecker Münster", [(3, "https://shop.dachdeckerei-muster.de/")], [])]
    assert run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])[0] is (
        CheckOutcome.PASSED
    )
    serps = [_serp("Dachdecker Münster", [(21, f"{SITE}/")], [])]
    assert run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])[0] is (
        CheckOutcome.FAILED
    )


def test_j1_a_list_of_only_the_search_engine_does_not_count_as_checked(
    raw_audit_full: RawAudit,
) -> None:
    serps = [
        _serp("Dachdecker Münster", [(2, f"{SITE}/")], []),
        _serp("Dachfenster Münster", [(1, "google.de")], []),
        _serp("Dachsanierung Münster", [(1, "www.google.com"), (2, "google.de")], []),
    ]
    outcome, measured = run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["positions"] == {"Dachdecker Münster": 2}
    assert measured["phrases_total"] == 1
    assert measured["phrases_evaluated"] == 1
    assert measured["phrases_missing"] == ["Dachfenster Münster", "Dachsanierung Münster"]


def test_j1_35_of_40_valid_lists_are_evaluated_and_the_five_are_named(
    raw_audit_full: RawAudit,
) -> None:
    serps = [_serp(f"Dachdecker Ort{number}", [(2, f"{SITE}/")], []) for number in range(35)] + [
        _serp(f"Dachfenster Ort{number}", [(1, "maps.google.com")], []) for number in range(5)
    ]
    outcome, measured = run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["phrases_evaluated"] == 35
    assert len(measured["phrases_missing"]) == 5
    assert measured["in_top"] == 35


def test_j1_without_any_valid_list_is_not_measured(raw_audit_full: RawAudit) -> None:
    serps = [_serp("Dachdecker Münster", [(1, "google.de")], [])]
    assert run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"]) == (
        CheckOutcome.NOT_MEASURABLE,
        {
            "reason": "no_valid_result_lists",
            "phrases_evaluated": 0,
            "phrases_missing": ["Dachdecker Münster"],
        },
    )


def test_j1_a_search_engine_page_among_real_results_is_still_a_list(
    raw_audit_full: RawAudit,
) -> None:
    serps = [_serp("Dachdecker Münster", [(1, "google.de"), (2, f"{SITE}/")], [])]
    measured = run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])[1]
    assert measured["positions"] == {"Dachdecker Münster": 2}
    assert measured["phrases_missing"] == []


def test_j2_local_pack_matching(raw_audit_full: RawAudit) -> None:
    serps = [
        _serp("Dachdecker Münster", [], [(2, "Dachdeckerei Muster e.K.")]),
        _serp("Dachsanierung Münster", [], [(1, "Dach Schmidt"), (4, "Dachdeckerei Muster")]),
        _serp("Dachfenster Münster", [], []),
    ]
    outcome, measured = run("j2", _with_serps(raw_audit_full, serps), THRESHOLDS["j2"])
    assert outcome is CheckOutcome.PARTIAL
    assert measured["positions"] == {"Dachdecker Münster": 2, "Dachsanierung Münster": 4}
    assert measured["in_top"] == 1
    assert measured["phrases_with_map"] == 2


@pytest.mark.parametrize("entry", ["Dachdeckerei", "Muster", "Dach"])
def test_j2_a_business_whose_name_is_only_part_of_ours_is_not_us(
    raw_audit_full: RawAudit, entry: str
) -> None:
    serps = [_serp("Dachdecker Münster", [], [(1, entry)])]
    outcome, measured = run("j2", _with_serps(raw_audit_full, serps), THRESHOLDS["j2"])
    assert outcome is CheckOutcome.FAILED
    assert measured["positions"] == {"Dachdecker Münster": None}


def test_j2_our_whole_name_with_further_words_is_us(raw_audit_full: RawAudit) -> None:
    serps = [_serp("Dachdecker Münster", [], [(2, "Dachdeckerei Muster Münster-Mitte")])]
    outcome, measured = run("j2", _with_serps(raw_audit_full, serps), THRESHOLDS["j2"])
    assert measured["positions"] == {"Dachdecker Münster": 2}
    assert outcome is not CheckOutcome.FAILED


def test_j2_not_in_pack_fails_and_no_pack_not_measurable(raw_audit_full: RawAudit) -> None:
    serps = [_serp("Dachdecker Münster", [], [(1, "Dach Schmidt")])]
    assert run("j2", _with_serps(raw_audit_full, serps), THRESHOLDS["j2"])[0] is (
        CheckOutcome.FAILED
    )
    serps = [_serp("Dachdecker Münster", [], [])]
    assert run("j2", _with_serps(raw_audit_full, serps), THRESHOLDS["j2"])[1] == {
        "reason": "no_local_pack"
    }


@pytest.mark.parametrize(
    ("position", "panel", "expected"),
    [
        (1, True, CheckOutcome.PASSED),
        (1, False, CheckOutcome.PASSED),
        (2, True, CheckOutcome.FAILED),
        (4, False, CheckOutcome.FAILED),
    ],
)
def test_j4_brand_position_and_panel(
    raw_audit_full: RawAudit, position: int, panel: bool, expected: CheckOutcome
) -> None:
    organic = [(position, f"{SITE}/")] if position == 1 else [(1, "facebook.com"), (position, SITE)]
    serps = [_serp("Dachdeckerei Muster", organic, [], knowledge_panel=panel)]
    outcome, measured = run("j4", _with_serps(raw_audit_full, serps), {})
    assert outcome is expected
    assert measured["position"] == position
    assert measured["knowledge_panel"] is panel


@pytest.mark.parametrize(
    ("position", "expected"), [(1, CheckOutcome.PASSED), (2, CheckOutcome.FAILED)]
)
def test_j4_leaves_the_panel_out_when_the_search_was_read_through_apify(
    raw_audit_full: RawAudit, position: int, expected: CheckOutcome
) -> None:
    organic = [(position, f"{SITE}/")] if position == 1 else [(1, "facebook.com"), (position, SITE)]
    serps = [_serp("Dachdeckerei Muster", organic, [], route="apify")]
    outcome, measured = run("j4", _with_serps(raw_audit_full, serps), {})
    assert outcome is expected
    assert measured == {
        "brand_phrase": "Dachdeckerei Muster",
        "position": position,
        "phrases_evaluated": 1,
        "phrases_missing": [],
    }


def test_j4_ignores_bing_brand_search(raw_audit_full: RawAudit) -> None:
    serps = [_serp("Dachdeckerei Muster", [(1, f"{SITE}/")], [], engine="bing")]
    assert run("j4", _with_serps(raw_audit_full, serps), {})[1] == {"reason": "no_brand_search"}


def test_j4_without_brand_search(raw_audit_full: RawAudit) -> None:
    raw = _with_serps(raw_audit_full, raw_audit_full.serps[:2])
    assert run("j4", raw, {})[1] == {"reason": "no_brand_search"}


def test_j6_overview_without_mention(raw_audit_full: RawAudit) -> None:
    serps = [
        serp.model_copy(
            update={"ai_overview_present": True, "ai_overview_mentions": ["Dach Schmidt"]}
        )
        for serp in raw_audit_full.serps[:1]
    ]
    serps.append(
        raw_audit_full.serps[1].model_copy(
            update={"ai_overview_present": True, "ai_overview_mentions": ["dachdeckerei-muster.de"]}
        )
    )
    outcome, measured = run("j6", _with_serps(raw_audit_full, serps), {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["phrases_mentioning_business"] == ["Dachsanierung Münster"]
    assert run("j6", _with_serps(raw_audit_full, serps[:1]), {})[0] is CheckOutcome.FAILED


def test_j1_counts_bare_domains_and_ignores_bing_and_images(raw_audit_full: RawAudit) -> None:
    serps = [
        _serp("Dachdecker Münster", [(1, "gelbeseiten.de"), (2, "dachdeckerei-muster.de")], []),
        _serp("Dachdecker Münster", [(30, "dachdeckerei-muster.de")], [], engine="bing"),
        _serp("Dachdecker Münster", [], [], vertical="images"),
    ]
    outcome, measured = run("j1", _with_serps(raw_audit_full, serps), THRESHOLDS["j1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["positions"] == {"Dachdecker Münster": 2}


def test_j7_bing_positions(raw_audit_full: RawAudit) -> None:
    serps = [
        _serp("Dachdecker Münster", [(4, "dachdeckerei-muster.de")], [], engine="bing"),
        _serp("Dachsanierung Münster", [(12, "dachdeckerei-muster.de")], [], engine="bing"),
        _serp("Dachdecker Münster", [(1, "dachdeckerei-muster.de")], []),
    ]
    outcome, measured = run("j7", _with_serps(raw_audit_full, serps), THRESHOLDS["j7"])
    assert outcome is CheckOutcome.PARTIAL
    assert measured["positions"] == {"Dachdecker Münster": 4, "Dachsanierung Münster": 12}
    failed = _with_serps(raw_audit_full, serps[1:2])
    assert run("j7", failed, THRESHOLDS["j7"])[0] is CheckOutcome.FAILED


def test_j7_without_bing_or_website(raw_audit_full: RawAudit) -> None:
    google_only = _with_serps(raw_audit_full, raw_audit_full.serps[:3])
    assert run("j7", google_only, THRESHOLDS["j7"])[1] == {"reason": "no_bing_serp"}
    no_site = raw_audit_full.model_copy(update={"website": None, "place": None})
    assert run("j7", no_site, THRESHOLDS["j7"])[1] == {"reason": "no_website"}


def test_j8_image_sources(raw_audit_full: RawAudit) -> None:
    serps = [
        _serp("Dachdecker Münster", [(1, "pinterest.de")], [], vertical="images"),
        _serp("Dachsanierung Münster", [(3, "dachdeckerei-muster.de")], [], vertical="images"),
    ]
    outcome, measured = run("j8", _with_serps(raw_audit_full, serps), {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["source_positions"] == {
        "Dachdecker Münster": None,
        "Dachsanierung Münster": 3,
    }
    assert run("j8", _with_serps(raw_audit_full, serps[:1]), {})[0] is CheckOutcome.FAILED
    assert run("j8", _with_serps(raw_audit_full, []), {})[1] == {"reason": "no_image_serp"}


def test_j2_takes_maps_positions_for_phrases_without_map_block(raw_audit_full: RawAudit) -> None:
    serps = [_serp("Dachdecker Münster", [], [(2, "Dachdeckerei Muster")])]
    rankings = [
        MapRanking(phrase="Dachdecker Münster", position=9, route="browser"),
        MapRanking(phrase="Dachfenster Münster", position=5, route="places_api"),
    ]
    raw = _with_serps(raw_audit_full, serps).model_copy(update={"map_rankings": rankings})

    outcome, measured = run("j2", raw, THRESHOLDS["j2"])

    assert outcome is CheckOutcome.PARTIAL
    assert measured["positions"] == {"Dachdecker Münster": 2, "Dachfenster Münster": 5}
    assert measured["maps_phrases"] == ["Dachfenster Münster"]
    assert measured["route"] == "places_api"


def test_j2_measures_from_maps_alone_when_google_search_was_blocked(
    raw_audit_full: RawAudit,
) -> None:
    rankings = [MapRanking(phrase="Dachdecker Münster", position=1, route="browser")]
    raw = _with_serps(raw_audit_full, []).model_copy(update={"map_rankings": rankings})

    outcome, measured = run("j2", raw, THRESHOLDS["j2"])

    assert outcome is CheckOutcome.PASSED
    assert measured["route"] == "browser"


def test_j3_names_the_routes_of_its_points(raw_audit_full: RawAudit) -> None:
    raw = _with_grid(raw_audit_full, [1, 2, 3])
    mixed = [
        point.model_copy(update={"route": "browser" if index else "places_api"})
        for index, point in enumerate(raw.grid)
    ]
    measured = run("j3", raw.model_copy(update={"grid": mixed}), {"grid": 5})[1]
    assert measured["routes"] == ["browser", "places_api"]
    assert "route" not in run("j3", raw, {"grid": 5})[1]


PREVIOUS_AUDIT = UUID("00000000-0000-4000-8000-0000000000a9")
PREVIOUS_AT = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def _with_history(raw: RawAudit, moves: list[tuple[int | None, int | None]]) -> RawAudit:
    history = RankingHistory(
        previous_audit_id=PREVIOUS_AUDIT,
        previous_requested_at=PREVIOUS_AT,
        changes=[
            PositionChange(phrase=f"begriff {index}", kind="organic", before=before, after=after)
            for index, (before, after) in enumerate(moves)
        ],
    )
    return raw.model_copy(update={"ranking_history": history})


@pytest.mark.parametrize(
    ("moves", "expected"),
    [
        ([(5, 3)], CheckOutcome.PASSED),
        ([(None, 8)], CheckOutcome.PASSED),
        ([(5, 3), (9, 1), (2, 9)], CheckOutcome.PASSED),
        ([(5, 3), (4, 6)], CheckOutcome.PARTIAL),
        ([(4, 4)], CheckOutcome.PARTIAL),
        ([(None, None)], CheckOutcome.FAILED),
        ([(3, 5)], CheckOutcome.FAILED),
        ([(3, None)], CheckOutcome.FAILED),
        ([(5, 3), (4, 6), (2, 9)], CheckOutcome.FAILED),
    ],
)
def test_development_compares_the_positions_with_the_previous_audit(
    raw_audit_full: RawAudit,
    moves: list[tuple[int | None, int | None]],
    expected: CheckOutcome,
) -> None:
    assert run("j9", _with_history(raw_audit_full, moves), {})[0] is expected


def test_development_records_what_moved(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("j9", _with_history(raw_audit_full, [(5, 3), (4, 6), (7, 7)]), {})

    assert outcome is CheckOutcome.PARTIAL
    assert measured["previous_audit_id"] == str(PREVIOUS_AUDIT)
    assert measured["previous_requested_at"] == "2026-10-13T09:00:00+00:00"
    assert (measured["improved"], measured["worsened"], measured["unchanged"]) == (1, 1, 1)
    assert measured["changes"] == [
        {"phrase": "begriff 0", "kind": "organic", "before": 5, "after": 3},
        {"phrase": "begriff 1", "kind": "organic", "before": 4, "after": 6},
        {"phrase": "begriff 2", "kind": "organic", "before": 7, "after": 7},
    ]


def test_development_without_shared_positions_is_not_measurable(raw_audit_full: RawAudit) -> None:
    assert run("j9", _with_history(raw_audit_full, []), {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "no_shared_rankings"},
    )
