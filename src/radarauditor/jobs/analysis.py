import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from radarauditor.config import Settings
from radarauditor.db.models import Job
from radarauditor.db.repositories import Repositories
from radarauditor.errors import AnalysisUnavailableError, ClaudeCodeError, HandoffError
from radarauditor.jobs import handoff
from radarauditor.jobs.claude_code import failure_reason, run_claude_code

logger = logging.getLogger(__name__)

INVALID_FILES_REASON = "Claude Code hat ungültige Dateien geschrieben"
BACKOFF = (timedelta(minutes=10), timedelta(minutes=30), timedelta(hours=2))
STEADY_INTERVAL = timedelta(hours=6)

ClaudeRunner = Callable[[Path, Settings], None]


def retry_at(attempts: int, now: datetime) -> datetime:
    step = attempts - 1
    if 0 <= step < len(BACKOFF):
        return now + BACKOFF[step]
    return now + STEADY_INTERVAL


def analyse_job(
    job: Job,
    repositories: Repositories,
    settings: Settings,
    runner: ClaudeRunner = run_claude_code,
) -> handoff.AnalysisRecord:
    folder = settings.radar_work_dir / handoff.ANALYSE_DIR / str(job.id)
    catalogue_json = handoff.load_catalogue_json(settings.catalogue_path)
    record = handoff.AnalysisRecord(path=handoff.AnalysisPath.CLAUDE_CODE)
    try:
        handoff.materialise(job, folder, catalogue_json, repositories)
        write_analysis(folder, settings, runner)
        done_root = settings.radar_work_dir / handoff.DONE_DIR
        handoff.deliver(folder, repositories, done_root, record)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    logger.info("Analyse job %s delivered by Claude Code", job.id)
    return record


def write_analysis(folder: Path, settings: Settings, runner: ClaudeRunner) -> None:
    for name in (handoff.FINDINGS_FILE, handoff.TEXTS_FILE, handoff.JUDGEMENTS_FILE):
        (folder / name).unlink(missing_ok=True)
    try:
        runner(folder, settings)
    except ClaudeCodeError as exc:
        logger.warning("Claude Code gave no analysis for %s: %s", folder.name, exc)
        raise AnalysisUnavailableError(failure_reason(exc.failure)) from exc
    try:
        handoff.validate_outputs(folder)
    except HandoffError as exc:
        logger.warning("Claude Code wrote invalid files for %s: %s", folder.name, exc)
        raise AnalysisUnavailableError(INVALID_FILES_REASON) from exc
