import logging
from uuid import UUID

from radarauditor.config import Settings
from radarauditor.jobs.claude_code import PROJECT_ONLY, absolute_rule_path, resolve_executable
from radarauditor.jobs.manager_agent import MANAGER_DIR, run_agent

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
TOOLS = "Bash,Read"
REPAIR_COMMANDS = ("status", "events", "inspect", "note", "step", "raw", "rerun-step")
PROMPT = """Du bist der Audit-Agent von RadarAuditor. Lies zuerst {skill} und halte dich daran.
Im Audit {audit_id} ist der Teil "{step}" gescheitert. Fehlermeldung: {reason}

Deine Aufgabe: Finde die Ursache und bring genau diesen Teil zum Laufen. Alles andere ist erledigt
und wird nicht wiederholt.
- `radarauditor audit step {audit_id} {step}` zeigt den gespeicherten Stand des Teils mit Warnungen.
- `radarauditor audit raw {audit_id}` listet die gespeicherten Rohantworten,
  `radarauditor audit raw {audit_id} <Name>` zeigt eine davon.
- `radarauditor audit rerun-step {audit_id} {step} --change "<was du an der Ursache geändert oder
  festgestellt hast>"` startet den Teil neu: zuerst ein einzelner Probeabruf, nur wenn der
  klappt der ganze Teil. Ein bezahlter Neuversuch ohne Angabe bei --change wird abgelehnt. Reicht
  das Guthaben nicht, meldet der Befehl den Fehlbetrag; dann vermerke das und beende deine Arbeit.
  Nach 3 bezahlten Neuversuchen eines Teils lehnt der Befehl weitere ab; dann vermerke das mit dem
  Grund und beende deine Arbeit. Die Teile ads_google und ads_meta hältst du nicht auf: Scheitern
  sie technisch, stehen p1 und p2 als nicht gemessen mit Grund im Ergebnis.
- Vermerke jeden Befund mit `radarauditor audit note {audit_id} "<Text>" --problem`.
Nichts erfinden, keine Messung weglassen. Antworte am Ende mit einer Zeile: erledigt oder nicht
erledigt und warum."""


def repair_command(
    executable: str, audit_id: UUID, step: str, reason: str, settings: Settings
) -> list[str]:
    skill = (settings.analysis_project_dir / SKILL_FILE).resolve()
    prompt = PROMPT.format(audit_id=audit_id, step=step, reason=reason, skill=skill)
    commands = [f"Bash(radarauditor audit {name}:*)" for name in REPAIR_COMMANDS]
    log_dir = absolute_rule_path((settings.radar_work_dir / MANAGER_DIR).resolve())
    command = [
        executable,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        *PROJECT_ONLY,
        "--tools",
        TOOLS,
    ]
    if settings.audit_manager_model:
        command += ["--model", settings.audit_manager_model]
    return [
        *command,
        "--allowedTools",
        *commands,
        f"Read({absolute_rule_path(skill)})",
        f"Read({log_dir}/**)",
    ]


async def run_repair_agent(audit_id: UUID, step: str, reason: str, settings: Settings) -> None:
    command = repair_command(resolve_executable(settings), audit_id, step, reason, settings)
    result = await run_agent(command, settings, settings.repair_timeout_seconds)
    logger.info("Repair agent of audit %s, step %s: %s", audit_id, step, result[:300])
