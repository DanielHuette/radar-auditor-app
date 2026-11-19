import json
from typing import Any

import pytest
from conftest import CATALOGUE_PATH, REPORT_ROOT
from test_control_agent import _bundles, _entry, _reply, _result, _verdict

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.config import Settings
from radarauditor.db.models import Verdict
from radarauditor.jobs import control_agent, control_data
from radarauditor.jobs.control_agent import ControlAnswerError
from radarauditor.jobs.control_data import RawAnswer
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


PARTS_FINDING = "Teil {part}: zwei von vier Seiten in Ordnung"


def _split_bundle(
    raw: RawAudit, catalogue: Catalogue, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> control_agent.Bundle:
    monkeypatch.setattr(control_data, "PART_CHARS", 200)
    bundle = _bundles(raw, catalogue, settings, [_result("b3")])[0]
    assert len(bundle.calls) > 1
    return bundle


def _asker(
    bundle: control_agent.Bundle, recomputed: str, seen: list[dict[str, Any]]
) -> control_agent.Ask:
    def ask(prompt: str, material: str) -> str:
        payload = json.loads(material)["checks"][0]
        seen.append(payload)
        if payload["part"] == control_agent.SYNTHESIS_PART:
            finding = payload["data"]["part_findings"][0]["findings"]
            evaluation = _verdict(
                reason="Rechenweg",
                evidence=f"part_findings[0].findings: {finding}",
                recomputed=recomputed,
            )
            return json.dumps({"checks": [{"check_id": "b3", "evaluation": evaluation}]})
        entry = _entry(bundle, "b3", payload["part"])
        entry["evaluation"] = {
            **entry["evaluation"],
            "findings": PARTS_FINDING.format(part=payload["part"]),
            "recomputed": "",
        }
        return _reply(entry)

    return ask


def test_the_total_of_a_split_check_is_recomputed_from_the_part_findings(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _split_bundle(raw_audit_full, catalogue, settings, monkeypatch)
    seen: list[dict[str, Any]] = []

    examined = control_agent.examine(bundle, _asker(bundle, "passed", seen))["b3"]

    synthesis = seen[-1]
    assert synthesis["part"] == control_agent.SYNTHESIS_PART
    findings = [entry["findings"] for entry in synthesis["data"]["part_findings"]]
    assert findings[0] == PARTS_FINDING.format(part=1)
    assert len(findings) == len(bundle.calls)
    assert examined.evaluation is not None
    assert examined.evaluation.ok is True


def test_a_wrong_total_of_a_split_check_is_a_cross(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _split_bundle(raw_audit_full, catalogue, settings, monkeypatch)

    examined = control_agent.examine(bundle, _asker(bundle, "failed", []))["b3"]

    assert examined.evaluation is not None
    assert examined.evaluation.ok is False
    assert "weicht" in (examined.evaluation.reason or "")


def test_a_split_check_with_a_crossed_part_is_not_sent_to_the_synthesis(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _split_bundle(raw_audit_full, catalogue, settings, monkeypatch)
    seen: list[dict[str, Any]] = []
    inner = _asker(bundle, "passed", seen)

    def ask(prompt: str, material: str) -> str:
        reply = inner(prompt, material)
        data = json.loads(reply)
        for entry in data.get("checks", []):
            if entry.get("part") == 1:
                entry["evaluation"] = _verdict(ok=False, reason="falsch gerechnet")
        return json.dumps(data)

    examined = control_agent.examine(bundle, ask)["b3"]

    assert all(payload["part"] != control_agent.SYNTHESIS_PART for payload in seen)
    assert examined.evaluation is not None
    assert examined.evaluation.ok is False


@pytest.mark.parametrize(
    "evidence",
    [
        "website.start_url: http",
        "website.start_url: https",
        "website.robots_skipped: []",
        "website.pages[0].status_code: 2e2",
        "website.pages[0].status_code: 2",
        "website.title: null",
        "website.title: -",
    ],
)
def test_trivial_evidence_is_not_accepted(evidence: str) -> None:
    data = {
        "website": {
            "start_url": "https://example.org/",
            "robots_skipped": [],
            "title": "Dachdecker Muster in Berlin",
            "pages": [{"url": "https://example.org/", "status_code": 200}],
        }
    }

    assert control_agent.evidence_holds(evidence, data) is False


def test_supplier_answers_reach_the_payload_of_their_check() -> None:
    answers = [RawAnswer("serp/google/organic", '{"items": [{"title": "Muster Dach"}]}')]
    data = control_data.Data({"customer": {"name": "Muster GmbH"}}, ())

    enriched = control_data._with_answers(data, answers)

    texts = [item["text"] for group in enriched.groups for item in group.items]
    assert texts == ['{"items": [{"title": "Muster Dach"}]}']


def test_supplier_answers_of_any_size_are_sent_in_parts_without_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_data, "PART_CHARS", 100)
    answers = [RawAnswer(f"serp_google_organic_{number}", "x" * 150) for number in range(4)]

    parts = control_data.step_parts("search_google", {"step": "search_google"}, answers)

    sent = [record["name"] for part in parts for record in part.data[control_data.SUPPLIER_ANSWERS]]
    assert sent == [answer.name for answer in answers]
    assert len(parts) == 4
    assert all(part.parts == 4 for part in parts)


def _step_reply(step: str, part: int, evidence: str, ok: bool = True, reason: str = "") -> str:
    collected = _verdict(ok=ok, reason=reason, evidence=evidence)
    return json.dumps({"steps": [{"step": step, "part": part, "collected": collected}]})


def _step_parts() -> list[control_data.StepPart]:
    answers = [RawAnswer("serp_google_organic_a", "Antwort " + "a" * 80)]
    return control_data.step_parts("search_google", {"step": "search_google"}, answers)


def test_the_collection_of_a_step_is_judged_once_over_all_its_answers() -> None:
    parts = _step_parts()
    asked: list[str] = []

    def ask(prompt: str, material: str) -> str:
        asked.append(material)
        return _step_reply("search_google", 1, "supplier_answers[0].text: Antwort aaaaaaaa")

    verdicts = control_agent.examine_steps(parts, ask)

    assert len(asked) == 1
    assert verdicts["search_google"].ok is True


def test_a_crossed_part_of_a_step_crosses_the_whole_step() -> None:
    answers = [RawAnswer(f"serp_google_organic_{number}", "x" * 150) for number in range(2)]
    original = control_data.PART_CHARS
    control_data.PART_CHARS = 100
    try:
        parts = control_data.step_parts("search_google", {"step": "search_google"}, answers)
    finally:
        control_data.PART_CHARS = original

    def ask(prompt: str, material: str) -> str:
        part = json.loads(material)["steps"][0]["part"]
        if part == 2:
            return _step_reply("search_google", part, "", ok=False, reason="falscher Ort")
        return _step_reply("search_google", part, "supplier_answers[0].text: xxxxxxxx")

    verdict = control_agent.examine_steps(parts, ask)["search_google"]

    assert verdict.ok is False
    assert "Teil 2" in (verdict.reason or "")


def test_a_step_verdict_without_proof_in_the_answers_is_a_cross() -> None:
    verdicts = control_agent.examine_steps(
        _step_parts(),
        lambda prompt, material: _step_reply(
            "search_google", 1, "supplier_answers[0].text: nirgends"
        ),
    )

    assert verdicts["search_google"].ok is False
    assert control_agent.NO_EVIDENCE_REASON in (verdicts["search_google"].reason or "")


def test_a_step_answer_for_another_step_is_an_answer_error() -> None:
    with pytest.raises(ControlAnswerError):
        control_agent.examine_steps(
            _step_parts(), lambda prompt, material: _step_reply("search_bing", 1, "x")
        )


def _examined(ok: bool = True) -> control_agent.Examined:
    ticked = Verdict(ok=True, reason=None, evidence="own: evidence")
    return control_agent.Examined(
        Verdict(ok=ok, reason="own", evidence="own: evidence"), ticked, ticked
    )


def test_the_verdict_of_a_step_replaces_the_collection_verdict_of_its_checks() -> None:
    step = Verdict(ok=True, reason=None, evidence="supplier_answers[0].text: Antwort aaaaaaaa")

    result = control_agent.apply_step_verdicts(
        {"j1": _examined(), "a2": _examined()},
        {"j1": ("search_google",), "a2": ("pagespeed",)},
        {"search_google": step},
    )

    assert result["j1"].collected is step
    assert result["a2"].collected.evidence == "own: evidence"


def test_a_crossed_step_crosses_every_check_of_the_step_and_empties_the_other_verdicts() -> None:
    step = Verdict(ok=False, reason="falscher Ort", evidence=None)

    result = control_agent.apply_step_verdicts(
        {"j1": _examined(), "j2": _examined(), "a2": _examined()},
        {"j1": ("search_google",), "j2": ("search_google", "place"), "a2": ("pagespeed",)},
        {"search_google": step},
    )

    for check_id in ("j1", "j2"):
        assert result[check_id].collected.ok is False
        assert "falscher Ort" in (result[check_id].collected.reason or "")
        assert result[check_id].data is None
        assert result[check_id].evaluation is None
    assert result["a2"].collected.ok is True


@pytest.mark.parametrize(
    "evidence",
    [
        "website.start_url: https://www.example.org/",
        "business.name: Katja Karsch",
        "website.pages[0].status_code: 200",
        "website.pages[0].url: https://www.example.org/kontakt",
    ],
)
def test_evidence_that_only_repeats_what_was_looked_at_is_not_accepted(
    evidence: str,
) -> None:
    data = {
        "website": {
            "start_url": "https://www.example.org/",
            "pages": [{"url": "https://www.example.org/kontakt", "status_code": 200}],
        },
        "business": {"name": "Katja Karsch"},
    }

    verdict = control_agent._verdict(control_agent._VerdictAnswer(ok=True, evidence=evidence), data)

    assert verdict.ok is False
    assert verdict.reason == control_agent.NO_EVIDENCE_REASON


def test_a_split_check_without_any_finding_gets_no_tick(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _split_bundle(raw_audit_full, catalogue, settings, monkeypatch)
    seen: list[dict[str, Any]] = []

    def ask(prompt: str, material: str) -> str:
        payload = json.loads(material)["checks"][0]
        seen.append(payload)
        if payload["part"] == control_agent.SYNTHESIS_PART:
            evaluation = _verdict(evidence="part_findings[0].of: 3", recomputed="passed")
            return json.dumps({"checks": [{"check_id": "b3", "evaluation": evaluation}]})
        return _reply(_entry(bundle, "b3", payload["part"]))

    examined = control_agent.examine(bundle, ask)["b3"]

    assert all(payload["part"] != control_agent.SYNTHESIS_PART for payload in seen)
    assert examined.evaluation is not None
    assert examined.evaluation.ok is False
    assert examined.evaluation.reason == control_agent.NO_FINDINGS_REASON


def test_a_total_proved_with_a_field_the_code_wrote_gets_no_tick(
    raw_audit_full: RawAudit,
    catalogue: Catalogue,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _split_bundle(raw_audit_full, catalogue, settings, monkeypatch)

    def ask(prompt: str, material: str) -> str:
        payload = json.loads(material)["checks"][0]
        if payload["part"] == control_agent.SYNTHESIS_PART:
            evaluation = _verdict(
                evidence="part_findings[0].of: " + str(payload["of"]), recomputed="passed"
            )
            return json.dumps({"checks": [{"check_id": "b3", "evaluation": evaluation}]})
        entry = _entry(bundle, "b3", payload["part"])
        entry["evaluation"] = {**entry["evaluation"], "findings": "Teil: Seite A ohne Titel"}
        return _reply(entry)

    examined = control_agent.examine(bundle, ask)["b3"]

    assert examined.evaluation is not None
    assert examined.evaluation.ok is False


def test_answers_of_a_step_are_found_by_the_step_name_in_the_stored_file_name() -> None:
    paths = [
        "a1/antworten/20261115T120000-00001-search_google-apify-serp.json",
        "a1/antworten/20261115T120000-00002-search_images-apify-serp.json",
        "a1/antworten/20261115T120000-00003-place-places_api-details.json",
        "a1/antworten/20261115T120000-00004-assistants-openai-search.txt",
    ]

    found = control_data.answer_paths(frozenset(), paths, "search_google")

    assert found == [paths[0]]
    assert control_data.answer_paths(frozenset(), paths, "place") == [paths[2]]
    assert control_data.answer_paths(frozenset(), paths) == []
