import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from conftest import AUDIT_ID, CATALOGUE_PATH, REPORT_ROOT

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.config import Settings
from radarauditor.db.models import CheckOutcome, CheckResult
from radarauditor.feedback.events import INVALID_REASON_CODE
from radarauditor.jobs import control_agent, control_data
from radarauditor.jobs.control_agent import (
    NO_EVIDENCE_REASON,
    NO_REASON,
    NO_RECOMPUTED_REASON,
    ControlAnswerError,
    Kind,
)
from radarauditor.jobs.control_data import ECHO_KEYS
from radarauditor.jobs.control_rules import load_rules
from radarauditor.scoring.catalog import Catalogue


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "catalogue_path": CATALOGUE_PATH,
            "routes_path": CATALOGUE_PATH.parent / "messwege.yaml",
            "templates_dir": REPORT_ROOT / "templates",
        }
    )


def _result(
    check_id: str,
    outcome: CheckOutcome = CheckOutcome.PASSED,
    measured: dict[str, object] | None = None,
) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="google",
        outcome=outcome,
        measured=measured if measured is not None else {"lcp_ms": 3100},
        weight=Decimal("1.000"),
    )


def _leaf(value: Any, path: str = "") -> tuple[str, Any] | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ECHO_KEYS:
                continue
            found = _leaf(child, f"{path}.{key}" if path else key)
            if found:
                return found
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            found = _leaf(child, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(value, str) and len(value) >= 4 or isinstance(value, int | float):
        return path, value
    return None


def _evidence(bundle: control_agent.Bundle, check_id: str, part: int = 1) -> str:
    item = next(
        item
        for call in bundle.calls
        for item in call
        if item.check_id == check_id and item.part == part
    )
    found = _leaf(item.data)
    assert found is not None
    return f"{found[0]}: {found[1]}"


def _verdict(
    ok: bool = True, reason: str = "", evidence: str = "", recomputed: str = ""
) -> dict[str, Any]:
    return {"ok": ok, "reason": reason, "evidence": evidence, "recomputed": recomputed}


def _entry(
    bundle: control_agent.Bundle, check_id: str, part: int = 1, **verdicts: dict[str, Any]
) -> dict[str, Any]:
    evidence = _evidence(bundle, check_id, part)
    outcome = bundle.outcomes[check_id].value
    entry = {
        "check_id": check_id,
        "part": part,
        "collected": _verdict(evidence=evidence),
        "data": _verdict(evidence=evidence),
        "evaluation": _verdict(evidence=evidence, recomputed=outcome),
    }
    return entry | verdicts


def _reply(*entries: dict[str, Any]) -> str:
    return json.dumps({"checks": list(entries)})


def _bundles(
    raw: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    results: list[CheckResult],
    size: int = 8,
) -> list[control_agent.Bundle]:
    return control_agent.bundles_of(
        results, raw, catalogue, size, settings.routes_path, settings.templates_dir
    )


def _bundle(
    raw: RawAudit, catalogue: Catalogue, settings: Settings, *check_ids: str
) -> control_agent.Bundle:
    results = [_result(check_id) for check_id in check_ids]
    bundles = _bundles(raw, catalogue, settings, results, len(results))
    assert len(bundles) == 1
    return bundles[0]


def _examine(
    bundle: control_agent.Bundle, reply: str, previous_error: str | None = None
) -> dict[str, control_agent.Examined]:
    return control_agent.examine(bundle, lambda prompt, material: reply, previous_error)


def test_three_verdicts_with_evidence_from_the_check_data_are_kept(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")

    examined = _examine(bundle, _reply(_entry(bundle, "a2")))["a2"]

    assert examined.collected.ok is True
    assert examined.data is not None
    assert examined.data.ok is True
    assert examined.evaluation is not None
    assert examined.evaluation.ok is True


def test_a_tick_without_evidence_becomes_a_cross_without_proof(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")

    examined = _examine(bundle, _reply(_entry(bundle, "a2", data=_verdict())))["a2"]

    assert examined.data is not None
    assert (examined.data.ok, examined.data.reason) == (False, NO_EVIDENCE_REASON)
    assert examined.evaluation is not None
    assert examined.evaluation.ok is True


def test_evidence_quoted_from_the_rule_is_not_accepted(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    item = bundle.calls[0][0]
    quote = f"rule: {item.payload['rule'][:40]}"

    examined = _examine(bundle, _reply(_entry(bundle, "a2", data=_verdict(evidence=quote))))["a2"]

    assert examined.data is not None
    assert (examined.data.ok, examined.data.reason) == (False, NO_EVIDENCE_REASON)


def test_evidence_with_a_wrong_value_at_an_existing_path_is_rejected(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    path = _evidence(bundle, "a2").split(":")[0]

    examined = _examine(
        bundle, _reply(_entry(bundle, "a2", data=_verdict(evidence=f"{path}: 987654321")))
    )["a2"]

    assert examined.data is not None
    assert examined.data.ok is False


def test_a_failed_first_verdict_leaves_the_other_two_empty(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")

    examined = _examine(
        bundle,
        _reply(_entry(bundle, "a2", collected=_verdict(ok=False, reason="no answer"))),
    )["a2"]

    assert (examined.collected.ok, examined.collected.reason) == (False, "no answer")
    assert examined.data is None
    assert examined.evaluation is None


def test_a_failed_verdict_without_a_reason_gets_a_stated_reason(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")

    examined = _examine(bundle, _reply(_entry(bundle, "a2", data=_verdict(ok=False))))["a2"]

    assert examined.data is not None
    assert examined.data.reason == NO_REASON


def test_a_recomputed_result_that_differs_from_the_calculated_one_is_a_cross(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    evidence = _evidence(bundle, "a2")

    examined = _examine(
        bundle,
        _reply(
            _entry(
                bundle,
                "a2",
                evaluation=_verdict(evidence=evidence, recomputed="failed", reason="slow"),
            )
        ),
    )["a2"]

    assert examined.evaluation is not None
    assert examined.evaluation.ok is False
    assert "weicht" in (examined.evaluation.reason or "")


def test_a_missing_recomputed_result_is_a_cross(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    evidence = _evidence(bundle, "a2")

    examined = _examine(
        bundle, _reply(_entry(bundle, "a2", evaluation=_verdict(evidence=evidence)))
    )["a2"]

    assert examined.evaluation is not None
    assert (examined.evaluation.ok, examined.evaluation.reason) == (False, NO_RECOMPUTED_REASON)


@pytest.mark.parametrize(
    "reply",
    ["not json", "", '{"checks": []}', '{"other": 1}', '{"checks": "x"}'],
)
def test_an_unusable_answer_is_an_answer_error(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings, reply: str
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")

    with pytest.raises(ControlAnswerError):
        _examine(bundle, reply)


def test_a_check_missing_in_the_answer_is_an_answer_error(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2", "a4")

    with pytest.raises(ControlAnswerError, match="a4"):
        _examine(bundle, _reply(_entry(bundle, "a2")))


def test_the_answer_is_found_inside_text_and_code_fences(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    fenced = "\n```json\n" + _reply(_entry(bundle, "a2")) + "\n```\nDone."
    reply = "Here is my result {not json}" + fenced

    assert _examine(bundle, reply)["a2"].collected.ok is True


def test_null_and_number_fields_in_the_answer_are_tolerated(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    entry = _entry(bundle, "a2")
    entry["collected"] = {**entry["collected"], "reason": None, "extra": 5}
    entry["unknown"] = True

    assert _examine(bundle, _reply(entry))["a2"].collected.ok is True


def test_the_last_format_error_is_handed_over_in_the_next_prompt(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")
    prompts: list[str] = []

    def ask(prompt: str, material: str) -> str:
        prompts.append(prompt)
        return _reply(_entry(bundle, "a2"))

    control_agent.examine(bundle, ask, "missing key checks")

    assert "missing key checks" in prompts[0]
    assert control_agent.PROMPT in prompts[0]


def test_the_prompt_demands_comparison_real_data_recomputation_and_no_rubber_stamping() -> None:
    prompt = control_agent.PROMPT

    for required in ("Anfrage", "echte Daten", "selbst aus", "Abnicken ist verboten"):
        assert required in prompt


def test_a_package_holds_request_data_rule_and_calculated_result(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    bundle = _bundle(raw_audit_full, catalogue, settings, "a2")

    payload = bundle.calls[0][0].payload

    assert {"request", "data", "rule", "calculated_result", "part", "of"} <= set(payload)
    assert payload["calculated_result"]["outcome"] == "passed"


@pytest.mark.parametrize(("size", "bundles"), [(1, 3), (2, 2), (3, 1), (4, 1)])
def test_checks_are_split_into_bundles_of_the_configured_size(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    size: int,
    bundles: int,
) -> None:
    results = [_result(check_id) for check_id in ("a2", "a4", "a7")]

    assert len(_bundles(raw_audit_full, catalogue, settings, results, size)) == bundles


def test_skipped_and_not_applicable_checks_are_not_controlled(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    results = [
        _result("a2", CheckOutcome.SKIPPED),
        _result("a4", CheckOutcome.NOT_APPLICABLE),
    ]

    assert _bundles(raw_audit_full, catalogue, settings, results) == []


@pytest.mark.parametrize("check_id", ["m1", "m2", "p5", "p6", "b12", "j9"])
def test_planned_not_measurable_checks_get_no_control(
    check_id: str,
) -> None:
    measured = {"reason": "not_verifiable_from_outside"}
    result = _result(check_id, CheckOutcome.NOT_MEASURABLE, measured)

    assert control_agent.control_kind(result) is Kind.NONE
    assert control_agent.needs_control(result) is False


def test_a_planned_reason_on_any_check_gets_no_control() -> None:
    result = _result("a2", CheckOutcome.NOT_MEASURABLE, {"reason": "no_access"})

    assert control_agent.control_kind(result) is Kind.NONE


def test_a_failed_measurement_gets_the_collection_control_only() -> None:
    result = _result("a2", CheckOutcome.NOT_MEASURABLE, {"reason": INVALID_REASON_CODE})

    assert control_agent.control_kind(result) is Kind.COLLECTION


def test_a_failed_measurement_needs_only_the_first_verdict(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    result = _result("a2", CheckOutcome.NOT_MEASURABLE, {"reason": INVALID_REASON_CODE})
    bundle = _bundles(raw_audit_full, catalogue, settings, [result])[0]
    entry = {
        "check_id": "a2",
        "part": 1,
        "collected": _verdict(ok=False, reason="the request failed"),
    }

    examined = _examine(bundle, _reply(entry))["a2"]

    assert examined.collected.ok is False
    assert examined.data is None
    assert examined.evaluation is None


def test_a_measured_check_is_fully_controlled() -> None:
    assert control_agent.control_kind(_result("a2")) is Kind.FULL


def test_the_fingerprint_changes_with_the_outcome_and_the_measured_values() -> None:
    base = _result("a2")

    assert control_agent.fingerprint(base) == control_agent.fingerprint(_result("a2"))
    assert control_agent.fingerprint(base) != control_agent.fingerprint(
        _result("a2", CheckOutcome.FAILED)
    )
    assert control_agent.fingerprint(base) != control_agent.fingerprint(
        _result("a2", measured={"lcp_ms": 1})
    )


def test_checks_are_sorted_by_measurement_so_a_package_is_homogeneous(
    raw_audit_full: RawAudit, catalogue: Catalogue, settings: Settings
) -> None:
    ids = ("a2", "a4", "a7", "b3")
    results = [_result(check_id) for check_id in reversed(ids)]

    bundle = _bundles(raw_audit_full, catalogue, settings, results)[0]

    definitions = {check.id: check for area in catalogue.areas for check in area.checks}
    measurements = [definitions[check_id].measurement for check_id in bundle.check_ids]
    assert measurements == sorted(measurements)


def test_data_that_is_too_large_is_split_into_parts_without_cutting(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_data, "PART_CHARS", 200)
    result = _result("b3")

    bundle = _bundles(raw_audit_full, catalogue, settings, [result])[0]

    items = [item for call in bundle.calls for item in call]
    assert len(items) > 1
    assert all(item.parts == len(items) for item in items)
    assert [item.part for item in items] == list(range(1, len(items) + 1))


def test_a_cross_in_any_part_gives_a_cross_for_the_whole_check(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_data, "PART_CHARS", 200)
    bundle = _bundles(raw_audit_full, catalogue, settings, [_result("b3")])[0]
    count = len(bundle.calls)
    assert count > 1

    def ask(prompt: str, material: str) -> str:
        part = json.loads(material)["checks"][0]["part"]
        entry = _entry(bundle, "b3", part)
        if part == 2:
            entry["data"] = _verdict(ok=False, reason="foreign data")
        return _reply(entry)

    examined = control_agent.examine(bundle, ask)["b3"]

    assert examined.data is not None
    assert examined.data.ok is False
    assert "Teil 2" in (examined.data.reason or "")


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("website.pages[1].status_code: 404", True),
        ("website.pages[0].url: https://example.org/a", True),
        ("website.pages[1].status_code: '404'", True),
        ("website.pages[2].status_code: 404", False),
        ("website.pages[1].status_code: 200", False),
        ("website.pages[1].status_code: ", False),
        ("website.pages[1].status_code: null", False),
        ("website.pages[1].missing: 404", False),
        ("just a quote", False),
        ("", False),
        ("website.flags.a.b: true", True),
        ("website.flags.a.b: false", False),
        ("website.title: Dachdecker", True),
        ("website.title: x", False),
        ("data.website.pages[1].status_code: 404", True),
    ],
)
def test_evidence_must_be_a_field_path_with_the_value_found_there(
    evidence: str, expected: bool
) -> None:
    data = {
        "website": {
            "title": "Dachdecker Muster in Berlin",
            "pages": [
                {"url": "https://example.org/a", "status_code": 200},
                {"url": "https://example.org/b", "status_code": 404},
            ],
            "flags": {"a.b": True},
        }
    }

    assert control_agent.evidence_holds(evidence, data) is expected


def test_ask_claude_runs_the_configured_model_with_the_payload_on_stdin(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(
        prompt: str, config: Any, model: str, timeout: int, material: str | None = None
    ) -> str:
        seen.update(prompt=prompt, model=model, timeout=timeout, material=material)
        return "{}"

    monkeypatch.setattr(control_agent.claude_code, "ask_without_tools", fake)

    control_agent.ask_claude(settings)("the prompt", "the payload")

    assert seen == {
        "prompt": "the prompt",
        "model": settings.control_model,
        "timeout": settings.control_timeout_seconds,
        "material": "the payload",
    }


def test_every_catalogue_check_has_a_rule_in_plain_words(
    catalogue: Catalogue, settings: Settings
) -> None:
    rules = load_rules(settings.templates_dir / control_data.RULES_FILE, catalogue)

    assert {check.id for area in catalogue.areas for check in area.checks} == set(rules)
    assert all(len(text) > 20 for text in rules.values())


def test_the_planned_lists_of_the_app_match_the_control_agent() -> None:
    import yaml

    config_file = REPORT_ROOT / "config" / "kontrolle.yaml"
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    assert set(document["planned_checks"]) == control_agent.PLANNED_CHECKS
    assert set(document["planned_reasons"]) == control_agent.PLANNED_REASONS
    assert document["invalid_reason"] == INVALID_REASON_CODE
