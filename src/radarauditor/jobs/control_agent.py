import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.config import Settings
from radarauditor.db.models import CheckOutcome, CheckResult, Verdict
from radarauditor.errors import ClaudeCodeError
from radarauditor.feedback.events import INVALID_REASON_CODE, NOT_BUILT_REASON
from radarauditor.jobs import claude_code
from radarauditor.jobs.control_data import (
    ECHO_KEYS,
    RULES_FILE,
    Item,
    RawAnswer,
    StepPart,
    dump,
    items_of,
)
from radarauditor.jobs.control_rules import load_rules
from radarauditor.scoring.catalog import AGENT_PENDING_REASON, Catalogue, find_check

logger = logging.getLogger(__name__)

NO_EVIDENCE_REASON = "Kontrolle ohne Beleg"
NO_REASON = "Kontrolle ohne Begründung"
NO_RECOMPUTED_REASON = "Kontrolle ohne nachgerechnetes Ergebnis"
NO_FINDINGS_REASON = (
    "Kein Teil der Daten hat einen Befund geliefert, das Gesamtergebnis ist nicht nachrechenbar"
)
MAX_CALL_CHARS = 120_000
MIN_QUOTE_CHARS = 6
SYNTHESIS_PART = 0
EMPTY_VALUES = frozenset({"", "null", "none"})
DECIMAL_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")
URL_PREFIX = re.compile(r"^(?:https?:)?/*(?:www\.)?", re.IGNORECASE)
UNCONTROLLED_OUTCOMES = frozenset({CheckOutcome.SKIPPED, CheckOutcome.NOT_APPLICABLE})
PLANNED_REASONS = frozenset(
    {
        "no_access",
        "needs_history",
        "ownership_not_verifiable",
        "not_verifiable_from_outside",
        "ads_account_not_connected",
        "paid_disabled",
        "no_location",
        AGENT_PENDING_REASON,
        NOT_BUILT_REASON,
    }
)
PLANNED_CHECKS = frozenset({"m1", "m2", "p5", "p6", "b12", "j9"})
JUDGED_OUTCOMES = (CheckOutcome.PASSED, CheckOutcome.PARTIAL, CheckOutcome.FAILED)
KIND_INFO = "info"
EVIDENCE_FORM = re.compile(r"^\s*([^:\n]+?)\s*:\s*(.+?)\s*$", re.DOTALL)
INDEX_BRACKET = re.compile(r"\[(\d+)\]")
FINDING_EVIDENCE = re.compile(r"^\s*(?:data\.)?part_findings\[\d+\]\.findings\s*:")
LAST_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[\d+\])?$")
PROMPT = (
    "Du bist der Kontrollagent von RadarAuditor. Du misst nichts neu, bewertest nichts neu und "
    "änderst nichts; du prüfst nur. Auf der Standardeingabe steht JSON mit der Liste checks. Jede "
    "Prüfung enthält: request (Lieferant, Endpunkt, Parameter und das Messprotokoll des Abrufs), "
    "data (genau die Daten, aus denen diese Prüfung berechnet wurde, unverändert; supplier_answers "
    "sind die gespeicherten Antworten des Lieferanten, wie er sie geliefert hat; service_terms "
    "sind die Leistungswörter je Suchbegriff, mit denen gerechnet wurde), rule (die "
    "Bewertungsregel im Klartext mit ihren Grenzwerten), calculated_result (das Ergebnis, das der "
    "Code berechnet hat) und part und of (Teil und Zahl der Teile der Daten).\n"
    "Gib für jede Prüfung drei Urteile ab.\n"
    "collected, Daten korrekt eingeholt: Gleiche die Anfrage mit der Antwort ab. Passen "
    "Suchbegriff, Ort, Domain und Firma der gelieferten Daten zu request.parameters und zum "
    "Betrieb? Wurde beim richtigen Lieferanten das Richtige angefragt und kam eine "
    "brauchbare Antwort an?\n"
    "data, Daten korrekt: Prüfe, ob es echte Daten sind: keine Platzhalter, keine leeren, fremden "
    "oder in sich widersprüchlichen Daten, passend zum Betrieb.\n"
    "evaluation, Auswertung korrekt: Rechne das Ergebnis selbst aus den Daten nach der rule nach "
    "und schreibe dein Ergebnis in recomputed (passed, partial oder failed; bei kind info, einer "
    "Feststellung ohne Wertung, schreibe stattdessen kurz, was du feststellst). Vergleiche es mit "
    "calculated_result und nenne den Rechenweg in reason. Weicht dein Ergebnis ab, ist das ein "
    "Fehler: ok=false.\n"
    "Abnicken ist verboten. Prüfe jede Angabe selbst; im Zweifel ok=false mit Begründung.\n"
    "Ein Urteil mit ok=true braucht evidence im Format Feldpfad: Wert. Der Feldpfad beginnt bei "
    "data (ohne das Wort data), Felder stehen mit Punkt, Listenelemente mit [Nummer], zum Beispiel "
    "website.pages[3].status_code: 404. Der Wert muss dort genau so stehen. Zitate aus rule, name, "
    "calculated_result, request oder customer_question zählen nicht als Beleg. Ein Urteil mit "
    "ok=false braucht eine Begründung in reason in einfachem Deutsch.\n"
    "Hat eine Prüfung in calculated_result das Ergebnis not_measurable, ist die Messung "
    "gescheitert oder ungültig: Gib dann nur collected ab und lass data und evaluation weg. "
    "ok=false mit dem Grund aus calculated_result.measured und request.measurement_log, wenn die "
    "Messung wirklich gescheitert ist; ok=true mit Beleg nur, wenn der Abruf nachweislich korrekt "
    "war und die Ungültigkeit zu Unrecht gemeldet wurde.\n"
    "Ist of größer als 1, siehst du nur Teil part der Daten: Urteile über diesen Teil, lass "
    "recomputed leer, und beachte, dass ein ok=false in einem Teil für die ganze Prüfung gilt. "
    "Schreibe in evaluation.findings, was die rule für das Gesamtergebnis braucht: die Einträge "
    "dieses Teils, die nach der rule betroffen sind oder zählen, jeweils mit Adresse oder Namen "
    "und kurzem Grund, und die Zahlen, die die rule verlangt. Das Gesamtergebnis rechnet ein "
    "letzter Aufruf aus den Befunden aller Teile nach.\n"
    'Antworte ausschließlich mit einem JSON-Objekt der Form {"checks": [{"check_id": "...", '
    '"part": 1, "collected": {"ok": true, "reason": "", "evidence": "Feldpfad: Wert"}, '
    '"data": {...}, "evaluation": {"ok": true, "reason": "Rechenweg", "evidence": '
    '"Feldpfad: Wert", "recomputed": "passed"}}]} mit genau einem Eintrag je Prüfung und Teil.'
)

SYNTHESIS_PROMPT = (
    "Du bist der Kontrollagent von RadarAuditor und rechnest nur das Gesamtergebnis einer Prüfung "
    "nach, deren Daten in mehreren Teilen geprüft wurden. Auf der Standardeingabe steht JSON mit "
    "der Liste checks. Jede Prüfung enthält: rule (die Bewertungsregel im Klartext), "
    "calculated_result (das Ergebnis, das der Code berechnet hat), kind und data: die gemeinsamen "
    "Daten ohne die geteilten Listen und part_findings, die Befunde der Prüfer aller Teile.\n"
    "Rechne aus den Befunden aller Teile nach der rule das Gesamtergebnis selbst nach und schreibe "
    "es in evaluation.recomputed (passed, partial oder failed). Nenne den Rechenweg in reason und "
    "gib evidence im Format Feldpfad: Wert aus data an, zum Beispiel "
    "part_findings[0].findings: Text. Weicht dein Ergebnis vom calculated_result ab oder reichen "
    "die Befunde zum Nachrechnen nicht, ist das ein Fehler: ok=false mit Begründung in einfachem "
    "Deutsch. Abnicken ist verboten.\n"
    'Antworte ausschließlich mit einem JSON-Objekt der Form {"checks": [{"check_id": "...", '
    '"part": 0, "evaluation": {"ok": true, "reason": "Rechenweg", "evidence": '
    '"Feldpfad: Wert", "recomputed": "passed"}}]} mit genau einem Eintrag je Prüfung.'
)

STEP_PROMPT = (
    "Du bist der Kontrollagent von RadarAuditor und prüfst nur, ob die Daten eines Messschritts "
    "korrekt eingeholt wurden. Auf der Standardeingabe steht JSON mit der Liste steps. Jeder "
    "Eintrag enthält step, part und of (Teil und Zahl der Teile) und data: request ist die "
    "Anfrage (Lieferant, Endpunkt, Parameter, Messprotokoll, Betrieb), supplier_answers sind "
    "die gespeicherten Antworten der Lieferanten, genau wie sie geliefert wurden.\n"
    "Gleiche die Anfrage mit jeder Antwort ab: Passen Suchbegriff, Ort, Domain und Firma der "
    "Antwort zu den Parametern und zum Betrieb? Wurde beim richtigen Lieferanten das Richtige "
    "angefragt und kam eine brauchbare Antwort an? Das Urteil gilt für alle Prüfungen dieses "
    "Schritts. Ein ok=false in einem Teil gilt für den ganzen Schritt.\n"
    "Abnicken ist verboten. Ein Urteil mit ok=true braucht evidence im Format Feldpfad: Wert aus "
    "den Daten, der Pfad beginnt bei data (ohne das Wort data), zum Beispiel "
    "supplier_answers[0].text: Text. Ein Urteil mit ok=false braucht eine Begründung in reason in "
    "einfachem Deutsch.\n"
    'Antworte ausschließlich mit einem JSON-Objekt der Form {"steps": [{"step": "...", '
    '"part": 1, "collected": {"ok": true, "reason": "", "evidence": "Feldpfad: Wert"}}]} mit '
    "genau einem Eintrag je Schritt und Teil."
)

type Ask = Callable[[str, str], str]


class Kind(StrEnum):
    NONE = "none"
    COLLECTION = "collection"
    FULL = "full"


class ControlAnswerError(ClaudeCodeError):
    pass


class _VerdictAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool
    reason: str = ""
    evidence: str = ""
    recomputed: str = ""
    findings: str = ""

    @field_validator("reason", "evidence", "recomputed", "findings", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return dump(value)


class _CheckAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    check_id: str
    part: int = 1
    collected: _VerdictAnswer
    data: _VerdictAnswer | None = None
    evaluation: _VerdictAnswer | None = None


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    checks: list[_CheckAnswer] = Field(default_factory=list)


class _StepEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step: str
    part: int = 1
    collected: _VerdictAnswer


class _StepReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    steps: list[_StepEntry] = Field(default_factory=list)


class _SynthesisCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    check_id: str
    evaluation: _VerdictAnswer


class _SynthesisAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    checks: list[_SynthesisCheck] = Field(default_factory=list)


@dataclass(frozen=True)
class Examined:
    collected: Verdict
    data: Verdict | None
    evaluation: Verdict | None


@dataclass(frozen=True)
class Bundle:
    check_ids: tuple[str, ...]
    calls: tuple[tuple[Item, ...], ...]
    kinds: dict[str, Kind]
    outcomes: dict[str, CheckOutcome]
    fingerprints: dict[str, str]


def is_planned(result: CheckResult) -> bool:
    reason = result.measured.get("reason")
    if reason == INVALID_REASON_CODE:
        return False
    return reason in PLANNED_REASONS or result.check_id in PLANNED_CHECKS


def control_kind(result: CheckResult) -> Kind:
    if result.outcome in UNCONTROLLED_OUTCOMES:
        return Kind.NONE
    if result.outcome is CheckOutcome.NOT_MEASURABLE:
        return Kind.NONE if is_planned(result) else Kind.COLLECTION
    return Kind.FULL


def needs_control(result: CheckResult) -> bool:
    return control_kind(result) is not Kind.NONE


def fingerprint(result: CheckResult) -> str:
    body = json.dumps(
        {"outcome": result.outcome.value, "measured": result.measured},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def ask_claude(settings: Settings) -> Ask:
    def ask(prompt: str, material: str) -> str:
        return claude_code.ask_without_tools(
            prompt,
            settings,
            settings.control_model,
            settings.control_timeout_seconds,
            material=material,
        )

    return ask


def bundles_of(
    results: Sequence[CheckResult],
    raw: RawAudit,
    catalogue: Catalogue,
    size: int,
    routes_path: Path,
    templates_dir: Path,
    answers: Mapping[str, Sequence[RawAnswer]] | None = None,
) -> list[Bundle]:
    rules = load_rules(templates_dir / RULES_FILE, catalogue)
    pending = [result for result in results if needs_control(result)]
    entries: list[tuple[CheckResult, list[Item]]] = []
    ordered = sorted(pending, key=lambda item: (_measurement_of(item, catalogue), item.check_id))
    for result in ordered:
        definition = find_check(catalogue, result.check_id)
        parts = items_of(
            definition,
            result.outcome.value,
            result.measured,
            raw,
            routes_path,
            rules,
            (answers or {}).get(result.check_id, ()),
        )
        entries.append((result, parts))
    return _pack(entries, size)


def _measurement_of(result: CheckResult, catalogue: Catalogue) -> str:
    return find_check(catalogue, result.check_id).measurement


def _pack(entries: list[tuple[CheckResult, list[Item]]], size: int) -> list[Bundle]:
    bundles: list[Bundle] = []
    current: list[tuple[CheckResult, list[Item]]] = []
    used = 0
    for entry in entries:
        weight = sum(len(item.text) for item in entry[1])
        alone = len(entry[1]) > 1 or weight > MAX_CALL_CHARS
        if alone or len(current) >= size or (current and used + weight > MAX_CALL_CHARS):
            if current:
                bundles.append(_bundle(current))
            current, used = [], 0
        if alone:
            bundles.append(_bundle([entry]))
            continue
        current.append(entry)
        used += weight
    if current:
        bundles.append(_bundle(current))
    return bundles


def _bundle(entries: list[tuple[CheckResult, list[Item]]]) -> Bundle:
    single = len(entries) == 1 and len(entries[0][1]) > 1
    calls = (
        tuple((item,) for item in entries[0][1])
        if single
        else (tuple(item for _, parts in entries for item in parts),)
    )
    return Bundle(
        tuple(result.check_id for result, _ in entries),
        calls,
        {result.check_id: control_kind(result) for result, _ in entries},
        {result.check_id: result.outcome for result, _ in entries},
        {result.check_id: fingerprint(result) for result, _ in entries},
    )


def examine(bundle: Bundle, ask: Ask, previous_error: str | None = None) -> dict[str, Examined]:
    prompt = PROMPT if previous_error is None else _with_error(previous_error, PROMPT)
    by_check: dict[str, list[Examined]] = {check_id: [] for check_id in bundle.check_ids}
    given: dict[str, list[tuple[Item, _CheckAnswer]]] = {
        check_id: [] for check_id in bundle.check_ids
    }
    for call in bundle.calls:
        payload = dump({"checks": [item.payload for item in call]})
        answers = _parse(ask(prompt, payload), [(item.check_id, item.part) for item in call])
        for item in call:
            answer = answers[(item.check_id, item.part)]
            by_check[item.check_id].append(_examined(answer, item, bundle))
            given[item.check_id].append((item, answer))
    merged = {check_id: _merged(parts) for check_id, parts in by_check.items()}
    for check_id, parts in given.items():
        if len(parts) > 1:
            merged[check_id] = _recomputed_total(
                merged[check_id], parts, bundle.outcomes[check_id], ask, previous_error
            )
    return merged


def examine_steps(
    parts: Sequence[StepPart], ask: Ask, previous_error: str | None = None
) -> dict[str, Verdict]:
    prompt = STEP_PROMPT if previous_error is None else _with_error(previous_error, STEP_PROMPT)
    by_step: dict[str, list[Verdict]] = {}
    for part in parts:
        reply = ask(prompt, dump({"steps": [part.payload]}))
        answer = _parse_step(reply, part)
        by_step.setdefault(part.step, []).append(_verdict(answer, part.data))
    return {step: _merge(verdicts) for step, verdicts in by_step.items()}


def apply_step_verdicts(
    examined: dict[str, Examined],
    steps_of: Mapping[str, Sequence[str]],
    verdicts: Mapping[str, Verdict],
) -> dict[str, Examined]:
    result: dict[str, Examined] = {}
    for check_id, own in examined.items():
        relevant = [verdicts[step] for step in steps_of.get(check_id, ()) if step in verdicts]
        if not relevant:
            result[check_id] = own
            continue
        crossed = [verdict for verdict in relevant if not verdict.ok]
        if crossed:
            reason = "; ".join(verdict.reason or NO_REASON for verdict in crossed)
            result[check_id] = Examined(
                Verdict(ok=False, reason=reason, evidence=crossed[0].evidence), None, None
            )
        elif not own.collected.ok:
            result[check_id] = own
        else:
            result[check_id] = Examined(relevant[0], own.data, own.evaluation)
    return result


def _parse_step(reply: str, part: StepPart) -> _VerdictAnswer:
    try:
        answer = _StepReply.model_validate(_json_object(reply, "steps"))
    except ValidationError as error:
        raise ControlAnswerError(f"the control answer has the wrong shape: {error}") from error
    given = [(entry.step, entry.part) for entry in answer.steps]
    if given != [(part.step, part.part)]:
        raise ControlAnswerError(
            f"the control answer covers {given or 'no step'}, expected {part.step} part {part.part}"
        )
    return answer.steps[0].collected


def _with_error(previous_error: str, prompt: str) -> str:
    return (
        f"Deine letzte Antwort war formal unbrauchbar: {previous_error}\n"
        "Antworte diesmal genau im verlangten Format.\n\n" + prompt
    )


def _recomputed_total(
    merged: Examined,
    parts: list[tuple[Item, _CheckAnswer]],
    outcome: CheckOutcome,
    ask: Ask,
    previous_error: str | None,
) -> Examined:
    items = [item for item, _ in parts]
    judged = outcome in JUDGED_OUTCOMES and items[0].payload["kind"] != KIND_INFO
    if not judged or merged.evaluation is None or not merged.evaluation.ok:
        return merged
    findings = [
        answer.evaluation.findings if answer.evaluation is not None else "" for _, answer in parts
    ]
    if not any(finding.strip() for finding in findings):
        return Examined(merged.collected, merged.data, Verdict(ok=False, reason=NO_FINDINGS_REASON))
    item = _synthesis_item(items, findings)
    prompt = SYNTHESIS_PROMPT
    if previous_error is not None:
        prompt = _with_error(previous_error, SYNTHESIS_PROMPT)
    reply = ask(prompt, dump({"checks": [item.payload]}))
    verdict = _evaluation(_parse_synthesis(reply, item.check_id), item, outcome)
    if verdict.ok and not FINDING_EVIDENCE.match(verdict.evidence or ""):
        verdict = Verdict(ok=False, reason=NO_EVIDENCE_REASON, evidence=verdict.evidence)
    return Examined(merged.collected, merged.data, verdict)


def _synthesis_item(items: list[Item], findings: list[str]) -> Item:
    first = items[0]
    data = {
        **first.common,
        "part_findings": [
            {"part": item.part, "of": item.parts, "findings": text}
            for item, text in zip(items, findings, strict=True)
        ],
    }
    header = {
        key: first.payload[key]
        for key in ("check_id", "name", "kind", "customer_question", "rule", "calculated_result")
    }
    payload = {**header, "part": SYNTHESIS_PART, "of": 1, "data": data}
    return Item(first.check_id, SYNTHESIS_PART, 1, payload, data, dump(payload), first.common)


def _parse_synthesis(reply: str, check_id: str) -> _VerdictAnswer:
    try:
        answer = _SynthesisAnswer.model_validate(_json_object(reply))
    except ValidationError as error:
        raise ControlAnswerError(f"the control answer has the wrong shape: {error}") from error
    given = [item.check_id for item in answer.checks]
    if given != [check_id]:
        raise ControlAnswerError(
            f"the control answer covers {', '.join(given) or 'no check'}, expected {check_id}"
        )
    return answer.checks[0].evaluation


def _json_object(reply: str, key: str = "checks") -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", reply):
        try:
            value, _ = decoder.raw_decode(reply[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and key in value:
            return value
    raise ControlAnswerError(f"the control answer contains no JSON object with the key {key}")


def _parse(reply: str, expected: Sequence[tuple[str, int]]) -> dict[tuple[str, int], _CheckAnswer]:
    try:
        answer = _Answer.model_validate(_json_object(reply))
    except ValidationError as error:
        raise ControlAnswerError(f"the control answer has the wrong shape: {error}") from error
    given = [(item.check_id, item.part) for item in answer.checks]
    if sorted(given) != sorted(expected):
        raise ControlAnswerError(
            f"the control answer covers {_listed(given) or 'no check'}, "
            f"expected {_listed(expected)}"
        )
    return {(item.check_id, item.part): item for item in answer.checks}


def _listed(entries: Sequence[tuple[str, int]]) -> str:
    return ", ".join(f"{check_id} part {part}" for check_id, part in sorted(entries))


def _examined(answer: _CheckAnswer, item: Item, bundle: Bundle) -> Examined:
    collected = _verdict(answer.collected, item.data)
    kind = bundle.kinds[item.check_id]
    if not collected.ok or kind is Kind.COLLECTION:
        return Examined(collected, None, None)
    if answer.data is None or answer.evaluation is None:
        raise ControlAnswerError(f"the control answer for {item.check_id} lacks data or evaluation")
    evaluation = _evaluation(answer.evaluation, item, bundle.outcomes[item.check_id])
    return Examined(collected, _verdict(answer.data, item.data), evaluation)


def _evaluation(answer: _VerdictAnswer, item: Item, outcome: CheckOutcome) -> Verdict:
    verdict = _verdict(answer, item.data)
    judged = outcome in JUDGED_OUTCOMES and item.payload["kind"] != KIND_INFO
    if not verdict.ok or item.parts > 1 or not judged:
        return verdict
    recomputed = answer.recomputed.strip().casefold()
    if not recomputed:
        return Verdict(ok=False, reason=NO_RECOMPUTED_REASON, evidence=verdict.evidence)
    if recomputed != outcome.value:
        reason = f"Nachgerechnetes Ergebnis {recomputed} weicht vom berechneten {outcome.value} ab"
        detail = answer.reason.strip()
        return Verdict(
            ok=False, reason=f"{reason}: {detail}" if detail else reason, evidence=verdict.evidence
        )
    return verdict


def _merged(parts: list[Examined]) -> Examined:
    if len(parts) == 1:
        return parts[0]
    collected = _merge([part.collected for part in parts])
    if not collected.ok:
        return Examined(collected, None, None)
    data = _merge([part.data for part in parts if part.data is not None])
    evaluation = _merge([part.evaluation for part in parts if part.evaluation is not None])
    return Examined(collected, data, evaluation)


def _merge(verdicts: list[Verdict]) -> Verdict:
    failed = [
        (number, verdict) for number, verdict in enumerate(verdicts, start=1) if not verdict.ok
    ]
    if not failed:
        first = verdicts[0]
        return Verdict(ok=True, reason=first.reason, evidence=first.evidence)
    reason = "; ".join(f"Teil {number}: {verdict.reason}" for number, verdict in failed)
    return Verdict(ok=False, reason=reason, evidence=failed[0][1].evidence)


def _verdict(answer: _VerdictAnswer, data: dict[str, Any]) -> Verdict:
    reason = answer.reason.strip() or None
    evidence = answer.evidence.strip() or None
    if not answer.ok:
        return Verdict(ok=False, reason=reason or NO_REASON, evidence=evidence)
    if evidence is None or not evidence_holds(evidence, data):
        return Verdict(ok=False, reason=NO_EVIDENCE_REASON, evidence=evidence)
    if _echo_only(evidence):
        return Verdict(ok=False, reason=NO_EVIDENCE_REASON, evidence=evidence)
    return Verdict(ok=True, reason=reason, evidence=evidence)


def _echo_only(evidence: str) -> bool:
    form = EVIDENCE_FORM.match(evidence)
    if form is None:
        return False
    last = LAST_KEY.search(form.group(1).strip())
    return last is not None and last.group(1) in ECHO_KEYS


def evidence_holds(evidence: str, data: dict[str, Any]) -> bool:
    form = EVIDENCE_FORM.match(evidence)
    if form is None:
        return False
    path, claimed = form.group(1), _unquoted(form.group(2))
    found, value = _resolve(data, path.removeprefix("data."))
    if not found or value is None or _empty(value):
        return False
    return _substantial(claimed, value) and _matches(value, claimed)


def _empty(value: Any) -> bool:
    return isinstance(value, list | dict | str) and not value


def _unquoted(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _resolve(data: Any, path: str) -> tuple[bool, Any]:
    tokens = [token for token in INDEX_BRACKET.sub(r".#\1", path).split(".") if token]
    return _walk(data, tokens)


def _walk(current: Any, tokens: list[str]) -> tuple[bool, Any]:
    if not tokens:
        return True, current
    if isinstance(current, list):
        head = tokens[0]
        if not (head.startswith("#") and head[1:].isdigit()) or int(head[1:]) >= len(current):
            return False, None
        return _walk(current[int(head[1:])], tokens[1:])
    if isinstance(current, dict):
        for size in range(len(tokens), 0, -1):
            key = ".".join(tokens[:size])
            if key in current:
                found, value = _walk(current[key], tokens[size:])
                if found:
                    return found, value
    return False, None


def _substantial(claimed: str, value: Any) -> bool:
    if claimed.casefold() in EMPTY_VALUES:
        return False
    if len(claimed) == 1:
        return claimed.isdigit() and isinstance(value, int) and not isinstance(value, bool)
    return True


def _matches(value: Any, claimed: str) -> bool:
    if isinstance(value, bool):
        return claimed.casefold() == str(value).casefold()
    if isinstance(value, int | float):
        return _same_number(value, claimed)
    if isinstance(value, str):
        wanted = _flat(claimed)
        actual = _flat(value)
        return wanted == actual or (_meaningful_quote(wanted) and wanted in actual)
    try:
        return bool(json.loads(claimed) == value)
    except json.JSONDecodeError:
        return False


def _meaningful_quote(text: str) -> bool:
    return len(URL_PREFIX.sub("", text)) >= MIN_QUOTE_CHARS


def _same_number(value: float, claimed: str) -> bool:
    if DECIMAL_NUMBER.fullmatch(claimed.strip()) is None:
        return False
    return float(claimed.strip().replace(",", ".")) == float(value)


def _flat(text: str) -> str:
    return " ".join(text.split())
