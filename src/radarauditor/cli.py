import asyncio
import functools
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from radarauditor.config import Settings, load_settings
from radarauditor.db.models import AuditKind, DocumentKind, JobKind
from radarauditor.db.repositories import Repositories
from radarauditor.errors import RadarError

DEFAULT_SOCIAL_CRAWL_PAGES = 20
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

app = typer.Typer(no_args_is_help=True)
worker_app = typer.Typer()
audit_app = typer.Typer()
jobs_app = typer.Typer()
report_app = typer.Typer()
document_app = typer.Typer()
google_app = typer.Typer()
social_app = typer.Typer()
keywords_app = typer.Typer()
app.add_typer(worker_app, name="worker")
app.add_typer(audit_app, name="audit")
app.add_typer(jobs_app, name="jobs")
app.add_typer(report_app, name="report")
app.add_typer(document_app, name="document")
app.add_typer(google_app, name="google")
app.add_typer(social_app, name="social")
app.add_typer(keywords_app, name="keywords")


def exits_on_error[**P](command: Callable[P, None]) -> Callable[P, None]:

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            command(*args, **kwargs)
        except RadarError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    return wrapper


def _context() -> tuple[Settings, Repositories]:
    settings = load_settings()
    return settings, Repositories(settings)


def _parse_uuid(value: str, name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not a valid id", param_hint=name) from exc


@app.command(help="Check settings and the Supabase connection; exit code 1 on failure.")
@exits_on_error
def health() -> None:
    _, repositories = _context()
    repositories.ping()
    typer.echo("ok")


@worker_app.command("run", help="Run the job loop until stopped.")
@exits_on_error
def worker_run() -> None:
    from radarauditor.jobs.worker import run_forever

    asyncio.run(run_forever(load_settings()))


@worker_app.command(
    "alive",
    help="Exit 1 unless the worker stored a heartbeat in the last three minutes (health check).",
)
@exits_on_error
def worker_alive() -> None:
    from radarauditor.jobs.heartbeat import ALIVE_MAX_AGE, alive_file_fresh

    alive_file = load_settings().worker_alive_file
    if not alive_file_fresh(alive_file, datetime.now(UTC)):
        minutes = int(ALIVE_MAX_AGE.total_seconds() // 60)
        typer.echo(f"no heartbeat in {alive_file} within {minutes} minutes", err=True)
        raise typer.Exit(code=1)
    typer.echo("ok")


@audit_app.command("run", help="Create an audit for a customer and enqueue collection.")
@exits_on_error
def audit_run(customer_id: str, with_access: bool = False) -> None:
    customer_uuid = _parse_uuid(customer_id, "customer-id")
    _, repositories = _context()
    customer = repositories.customer(customer_uuid)
    kind = AuditKind.WITH_ACCESS if with_access else AuditKind.REMOTE
    audit = repositories.create_audit(customer.id, kind)
    repositories.enqueue(JobKind.COLLECT, customer.id, audit.id)
    typer.echo(str(audit.id))


@audit_app.command(
    "run-jobs",
    help=(
        "Work through the due jobs of one audit (collect, score, analyse, report, retry of "
        "deferred Google searches) one after the other and end.\n\n"
        "Exit code 1 when a job of the audit has failed. The full log goes to the file named "
        "on the last line; the console shows only warnings and the steps. --retry-failed puts "
        "the failed jobs of the audit back into the queue first."
    ),
)
@exits_on_error
def audit_run_jobs(audit_id: str, retry_failed: bool = False) -> None:
    from radarauditor.db.models import JobStatus
    from radarauditor.jobs.audit_run import retry_failed as put_back
    from radarauditor.jobs.audit_run import run_audit_jobs
    from radarauditor.jobs.manager_agent import manager_log_path, restore_claude_login

    audit_uuid = _parse_uuid(audit_id, "audit-id")
    restore_claude_login()
    settings, repositories = _context()
    log_path = manager_log_path(settings, audit_uuid)
    _log_to_file(log_path)
    if retry_failed:
        kinds = put_back(audit_uuid, repositories)
        typer.echo(f"put back: {', '.join(kind.value for kind in kinds) or 'nothing failed'}")
    steps = asyncio.run(run_audit_jobs(audit_uuid, repositories, settings))
    for step in steps:
        typer.echo(step.line())
    jobs = repositories.jobs_of_audit(audit_uuid)
    waiting = [job for job in jobs if job.status is JobStatus.QUEUED]
    for job in waiting:
        until = f" not before {job.run_after.isoformat()}" if job.run_after else ""
        typer.echo(f"waiting: {job.kind.value}{until}")
    typer.echo(f"audit status: {repositories.audit(audit_uuid).status.value}")
    typer.echo(f"log: {log_path}")
    if any(job.status is JobStatus.FAILED for job in jobs):
        raise typer.Exit(code=1)


@audit_app.command("status", help="Show the status of an audit and of its jobs.")
@exits_on_error
def audit_status(audit_id: str) -> None:
    from radarauditor.jobs.audit_inspect import status_lines

    audit_uuid = _parse_uuid(audit_id, "audit-id")
    _, repositories = _context()
    audit = repositories.audit(audit_uuid)
    for line in status_lines(audit, repositories.jobs_of_audit(audit_uuid)):
        typer.echo(line)


@audit_app.command(
    "events",
    help="List the events of an audit; --problems shows only failed, suspended and open steps.",
)
@exits_on_error
def audit_events(audit_id: str, problems: bool = False) -> None:
    from radarauditor.jobs.audit_inspect import event_lines

    _, repositories = _context()
    rows = repositories.audit_event_rows(_parse_uuid(audit_id, "audit-id"))
    for line in event_lines(rows, problems_only=problems):
        typer.echo(line)


@audit_app.command(
    "inspect",
    help="Show what the analysis and the report of an audit contain, to check them for gaps.",
)
@exits_on_error
def audit_inspect(audit_id: str) -> None:
    from radarauditor.jobs.audit_inspect import inspect_lines, load_view
    from radarauditor.scoring.catalog import load_catalogue

    settings, repositories = _context()
    view = load_view(_parse_uuid(audit_id, "audit-id"), repositories)
    for line in inspect_lines(view, load_catalogue(settings.catalogue_path)):
        typer.echo(line)


@audit_app.command(
    "note",
    help="Write a note of the audit manager into the events; --problem marks an error.",
)
@exits_on_error
def audit_note(audit_id: str, text: str, problem: bool = False) -> None:
    from radarauditor.feedback.events import Result
    from radarauditor.jobs.audit_manager import NOTE_LABEL

    _, repositories = _context()
    result = Result.FAILED if problem else Result.OPEN
    repositories.add_manager_event(_parse_uuid(audit_id, "audit-id"), result, NOTE_LABEL, text)
    typer.echo("noted")


@audit_app.command(
    "conclude",
    help=(
        "Write the final verdict of the audit manager: --verdict is ok (complete and plausible), "
        "incomplete (measurements or parts missing) or failed."
    ),
)
@exits_on_error
def audit_conclude(
    audit_id: str, text: str, verdict: Annotated[str, typer.Option("--verdict")]
) -> None:
    from radarauditor.feedback.events import Result
    from radarauditor.jobs.audit_manager import CONCLUSION_LABEL

    results = {"ok": Result.MEASURED, "incomplete": Result.SUSPENDED, "failed": Result.FAILED}
    if verdict not in results:
        raise typer.BadParameter(
            f"{verdict!r} is not one of ok, incomplete, failed", param_hint="verdict"
        )
    _, repositories = _context()
    repositories.add_manager_event(
        _parse_uuid(audit_id, "audit-id"), results[verdict], CONCLUSION_LABEL, text
    )
    typer.echo("concluded")


@audit_app.command("step", help="Show the stored state of one measuring step of an audit.")
@exits_on_error
def audit_step(audit_id: str, step: str) -> None:
    from radarauditor.collectors.steps import StepStore

    _, repositories = _context()
    stored = StepStore(_parse_uuid(audit_id, "audit-id"), repositories).stored(step)
    if stored is None:
        raise RadarError(f"step {step} has not run in this audit")
    typer.echo(stored.model_dump_json(indent=2, exclude={"value"}))


@audit_app.command("raw", help="List the stored raw answers of an audit, or print the one named.")
@exits_on_error
def audit_raw(audit_id: str, name: Annotated[str | None, typer.Argument()] = None) -> None:
    from radarauditor.collectors.steps import RAW_BUCKET, raw_answer_path

    _, repositories = _context()
    audit_uuid = _parse_uuid(audit_id, "audit-id")
    folder = raw_answer_path(audit_uuid, "").rstrip("/")
    if name is None:
        for path in repositories.list_paths(RAW_BUCKET, folder):
            typer.echo(path.removeprefix(f"{folder}/"))
        return
    typer.echo(repositories.download_bytes(RAW_BUCKET, f"{folder}/{name}").decode("utf-8"))


@audit_app.command(
    "rerun-step",
    help=(
        "Run one failed measuring step of an audit again, first as a single probe call, then in "
        "full. A paid step needs --change with what was changed at the cause, and enough credit. "
        "Exit code 1 while the step still fails."
    ),
)
@exits_on_error
def audit_rerun_step(
    audit_id: str, step: str, change: Annotated[str, typer.Option("--change")] = ""
) -> None:
    from radarauditor.jobs.manager_agent import restore_claude_login
    from radarauditor.jobs.rerun import rerun_step
    from radarauditor.jobs.worker import audit_gate, read_worker_settings

    restore_claude_login()
    settings, repositories = _context()
    audit_uuid = _parse_uuid(audit_id, "audit-id")
    gate = audit_gate(read_worker_settings(repositories), repositories, audit_uuid)
    result = asyncio.run(rerun_step(audit_uuid, step, change, gate, repositories, settings))
    typer.echo(result.message)
    if not result.measured:
        raise typer.Exit(code=1)


@jobs_app.command(
    "pull", help="Write pending analysis jobs into the work directory for Claude Code."
)
@exits_on_error
def jobs_pull() -> None:
    from radarauditor.jobs.handoff import pull

    settings, repositories = _context()
    for folder in pull(repositories, settings):
        typer.echo(str(folder))


@jobs_app.command("push", help="Upload finished analyses from the work directory.")
@exits_on_error
def jobs_push() -> None:
    from radarauditor.jobs.handoff import push

    settings, repositories = _context()
    for folder in push(repositories, settings):
        typer.echo(str(folder))


@report_app.command("sales", help="Render the sales report PDF.")
@exits_on_error
def report_sales(audit_id: str) -> None:
    from radarauditor.reporting.report import build_sales_report

    audit_uuid = _parse_uuid(audit_id, "audit-id")
    settings, repositories = _context()
    typer.echo(str(build_sales_report(audit_uuid, repositories, settings)))


@report_app.command("monthly", help="Render the before-after report PDF.")
@exits_on_error
def report_monthly(audit_id: str) -> None:
    from radarauditor.reporting.report import build_monthly_report

    audit_uuid = _parse_uuid(audit_id, "audit-id")
    settings, repositories = _context()
    typer.echo(str(build_monthly_report(audit_uuid, repositories, settings)))


@document_app.command("build", help="Render a business document from a JSON data file.")
@exits_on_error
def document_build(kind: str, customer_id: str, data_file: str) -> None:
    from radarauditor.documents.builder import build_document

    document_kind = _parse_document_kind(kind)
    customer_uuid = _parse_uuid(customer_id, "customer-id")
    data = _read_data_file(Path(data_file))
    settings, repositories = _context()
    document = build_document(document_kind, customer_uuid, data, repositories, settings)
    typer.echo(document.file_path or str(document.id))


def _parse_document_kind(value: str) -> DocumentKind:
    try:
        return DocumentKind(value)
    except ValueError as exc:
        choices = ", ".join(kind.value for kind in DocumentKind)
        raise typer.BadParameter(f"{value!r} is not one of {choices}", param_hint="kind") from exc


def _read_data_file(path: Path) -> dict[str, object]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"{path}: {exc}", param_hint="data-file") from exc
    if not isinstance(content, dict):
        raise typer.BadParameter(f"{path}: must contain a JSON object", param_hint="data-file")
    return content


@google_app.command(
    "auth",
    help=(
        "Authorise the operator's Google account for audits with customer access.\n\n"
        "Prints the consent address and waits for Google's redirect on a local port; with "
        "--manual the address the browser ends on is pasted instead. The refresh token is "
        "stored in Supabase."
    ),
)
@exits_on_error
def google_auth(manual: bool = False, port: int = 0) -> None:
    from radarauditor.collectors.google_auth import authorise

    settings, repositories = _context()
    grant = authorise(
        settings,
        repositories,
        manual=manual,
        port=port,
        show=typer.echo,
        read_line=lambda: typer.prompt("redirect address", prompt_suffix=": "),
    )
    typer.echo(f"stored Google authorisation for {len(grant.granted_scopes)} scopes")
    for scope in grant.missing_scopes():
        typer.echo(f"not granted: {scope}", err=True)


@social_app.command(
    "check",
    help=(
        "Find the social media accounts of a website and read their figures and latest "
        "posts.\n\n"
        "Accounts linked on the website or in its legal notice count as the business's own; "
        "for the other platforms Google is searched through Apify and a hit counts only when "
        "the profile links to the website, shows the phone number, or carries the name and "
        "names the place. Costs Apify credit for every search and every account read."
    ),
)
@exits_on_error
def social_check(
    website: str,
    name: Annotated[
        list[str] | None,
        typer.Option("--name", help="Business or owner name to search for; repeat for several"),
    ] = None,
    max_pages: Annotated[int, typer.Option(min=1)] = DEFAULT_SOCIAL_CRAWL_PAGES,
) -> None:
    from radarauditor.collectors.patterns import parse_http_url
    from radarauditor.collectors.social import measure_website
    from radarauditor.collectors.social_summary import render_social

    start_url = parse_http_url(website)
    if start_url is None:
        raise typer.BadParameter(f"{website!r} is not a web address", param_hint="website")
    snapshot = asyncio.run(measure_website(load_settings(), start_url, name or [], max_pages))
    typer.echo(render_social(snapshot))


@keywords_app.command(
    "import",
    help=(
        "Store the search phrases of a Google Ads Keyword Planner export for a customer.\n\n"
        "Reads the column Keyword and the average monthly searches of the exported CSV file; "
        "of a range such as 1K - 10K the lower end is stored. The city of the customer at the "
        "start or end of a phrase is left out, because every search adds it; with --nationwide "
        "the phrases are stored as they stand and searched without a city. "
        "Phrases the customer already has keep their place and get the volume of the file; "
        "nothing is deleted. Every audit of the customer searches all stored phrases."
    ),
)
@exits_on_error
def keywords_import(
    customer_id: str,
    export_file: Path,
    nationwide: Annotated[
        bool, typer.Option("--nationwide", help="Store the phrases as nationwide keywords.")
    ] = False,
) -> None:
    from radarauditor.db.models import KeywordScope
    from radarauditor.keyword_import import import_keywords, read_planner_export

    customer_uuid = _parse_uuid(customer_id, "customer-id")
    try:
        planned = read_planner_export(export_file)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="export-file") from exc
    _, repositories = _context()
    count = import_keywords(
        repositories,
        repositories.customer(customer_uuid),
        planned,
        KeywordScope.NATIONWIDE if nationwide else KeywordScope.LOCAL,
    )
    typer.echo(
        f"{count.added} added, {count.already_stored} already stored, "
        f"{count.with_volume} with search volume"
    )


def _log_to_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setLevel(logging.WARNING)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    app()


if __name__ == "__main__":
    main()
