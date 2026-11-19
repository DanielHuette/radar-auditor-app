import logging
import mimetypes
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from postgrest import APIError, APIResponse, SyncPostgrestClient
from pydantic import SecretStr, ValidationError
from storage3.types import FileOptions
from storage3.utils import StorageException
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from radarauditor.config import Settings
from radarauditor.db.crm import (
    AssistantRequest,
    AssistantResult,
    CrmCustomer,
    CrmOrder,
    CrmProduct,
    CrmQuote,
    CrmQuoteItem,
    MailTriage,
)
from radarauditor.db.models import (
    Audit,
    AuditCost,
    AuditEstimate,
    AuditKind,
    AuditStatus,
    CheckResult,
    Competitor,
    ControlState,
    Customer,
    CustomerAccess,
    Document,
    Event,
    Finding,
    Job,
    JobKind,
    JobStatus,
    Keyword,
    KeywordScope,
    Location,
    OptimizationChange,
    OptimizationChangeStatus,
    ProspectSearch,
    Ranking,
    Verdict,
)
from radarauditor.errors import StorageError, UnreadableMailError
from radarauditor.feedback.events import AuditEvent, Result
from supabase import ClientOptions, create_client

logger = logging.getLogger(__name__)

DATABASE_TIMEOUT_SECONDS = 15
STORAGE_TIMEOUT_SECONDS = 60
PAGE_SIZE = 500
LISTING_LIMIT = 1000
CUSTOMER_ACCESS_COLUMNS = (
    "customer_id,google_business_profile_granted,search_console_property,wordpress_url,"
    "wordpress_user,granted_at,google_location_name,analytics_property_id"
)
CRM_SCHEMA = "crm"
CRM_REFUSAL_CODES = frozenset({"P0001", "P0002", "22023", "55000"})
WORDPRESS_PASSWORD_COLUMN = "wordpress_app_password_encrypted"
FINISHED_STATUSES = frozenset({AuditStatus.REPORTED, AuditStatus.FAILED})
CLIENT_ERRORS = (APIError, StorageException, httpx.HTTPError)
MISSING_SCHEMA_CODES = frozenset({"42703", "22P02", "PGRST204"})
CONSTRAINT_VIOLATION_CODE = "23514"
OLD_RADAR_AREAS = tuple("abcdefghijklmno")
SCHEMA_PROBES: tuple[tuple[str, str, str, tuple[str, str] | None], ...] = (
    (
        "023",
        "check_results",
        "provider,control_state,control_attempt,control_last_error,"
        "verdict_collected_ok,verdict_data_ok,verdict_evaluation_ok",
        None,
    ),
    ("023", "audit_events", "protocol", None),
    ("024", "audits", "id", ("status", AuditStatus.CONTROLLING.value)),
)


class MigrationMissingError(StorageError):
    pass


class ExecutableQuery(Protocol):
    def execute(self) -> APIResponse: ...


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)
def _execute_with_retry(query: ExecutableQuery) -> APIResponse:
    return query.execute()


def _rows(response: APIResponse) -> list[dict[str, Any]]:
    return [row for row in response.data if isinstance(row, dict)]


def _now() -> datetime:
    return datetime.now(UTC)


def _cost(response: APIResponse, source: str) -> Decimal:
    value: object = response.data
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise StorageError(f"cost of {source} returned {value!r}")
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise StorageError(f"cost of {source} returned {value!r}") from exc


def _claimed_mail(row: dict[str, Any]) -> MailTriage | UnreadableMailError:
    try:
        return MailTriage.model_validate(row)
    except ValidationError as exc:
        try:
            mail_id: UUID | None = UUID(str(row.get("id")))
        except ValueError:
            mail_id = None
        return UnreadableMailError(f"claimed mail {mail_id} has another shape: {exc}", mail_id)


class Repositories:
    def __init__(self, settings: Settings) -> None:
        options = ClientOptions(
            postgrest_client_timeout=DATABASE_TIMEOUT_SECONDS,
            storage_client_timeout=STORAGE_TIMEOUT_SECONDS,
        )
        self._client = create_client(
            str(settings.supabase_url),
            settings.supabase_service_key.get_secret_value(),
            options=options,
        )
        self._crm_client: SyncPostgrestClient | None = None

    def _crm(self) -> SyncPostgrestClient:
        if self._crm_client is None:
            self._crm_client = self._client.schema(CRM_SCHEMA)
        return self._crm_client

    def _run(self, query: ExecutableQuery, action: str, *, idempotent: bool = True) -> APIResponse:
        try:
            if idempotent:
                return _execute_with_retry(query)
            return query.execute()
        except CLIENT_ERRORS as exc:
            own = isinstance(exc, APIError) and exc.code in CRM_REFUSAL_CODES
            reason = exc.message if own and isinstance(exc, APIError) else None
            raise StorageError(f"{action} failed: {exc}", reason) from exc

    def _one(self, table: str, column: str, value: str, action: str) -> dict[str, Any]:
        query = self._client.table(table).select("*").eq(column, value).limit(1)
        rows = _rows(self._run(query, action))
        if not rows:
            raise StorageError(f"{action} failed: no row in {table} with {column}={value}")
        return rows[0]

    def _by_customer(self, table: str, customer_id: UUID) -> list[dict[str, Any]]:
        query = self._client.table(table).select("*").eq("customer_id", str(customer_id))
        return _rows(self._run(query, f"load {table} of customer {customer_id}"))

    def ping(self) -> None:
        self._run(self._client.table("settings").select("key").limit(1), "health check")

    def missing_migrations(self) -> list[str]:
        missing: list[str] = []
        for number, table, columns, condition in SCHEMA_PROBES:
            query = self._client.table(table).select(columns).limit(1)
            if condition is not None:
                query = query.eq(*condition)
            try:
                _execute_with_retry(query)
            except APIError as exc:
                if exc.code not in MISSING_SCHEMA_CODES:
                    raise StorageError(f"check of migration {number} failed: {exc}") from exc
                if number not in missing:
                    missing.append(number)
            except CLIENT_ERRORS as exc:
                raise StorageError(f"check of migration {number} failed: {exc}") from exc
        if self._old_radar_areas_left() and "030" not in missing:
            missing.append("030")
        return missing

    def _old_radar_areas_left(self) -> bool:
        query = (
            self._crm()
            .table("products")
            .select("id")
            .overlaps("radar_areas", list(OLD_RADAR_AREAS))
            .limit(1)
        )
        try:
            return bool(_rows(_execute_with_retry(query)))
        except APIError as exc:
            if exc.code in MISSING_SCHEMA_CODES:
                return False
            raise StorageError(f"check of migration 030 failed: {exc}") from exc
        except CLIENT_ERRORS as exc:
            raise StorageError(f"check of migration 030 failed: {exc}") from exc

    def customer(self, customer_id: UUID) -> Customer:
        row = self._one("customers", "id", str(customer_id), f"load customer {customer_id}")
        return Customer.model_validate(row)

    def settings_value(self, key: str) -> object | None:
        query = self._client.table("settings").select("value").eq("key", key).limit(1)
        rows = _rows(self._run(query, f"load setting {key}"))
        return rows[0].get("value") if rows else None

    def settings_values(self, keys: Sequence[str]) -> dict[str, object]:
        query = self._client.table("settings").select("key,value").in_("key", list(keys))
        rows = _rows(self._run(query, f"load settings {', '.join(keys)}"))
        return {str(row["key"]): row.get("value") for row in rows if "key" in row}

    def consume_source_quota(self, source: str, cap: int) -> bool:
        query = self._client.rpc("consume_source_quota", {"p_source": source, "p_cap": cap})
        response = self._run(query, f"consume quota of {source}", idempotent=False)
        allowed: object = response.data
        if isinstance(allowed, list):
            allowed = allowed[0] if allowed else None
        if not isinstance(allowed, bool):
            raise StorageError(f"consume quota of {source} returned {allowed!r}")
        return allowed

    def month_source_cost(self, source: str) -> Decimal:
        query = self._client.rpc("source_month_cost", {"p_source": source})
        return _cost(self._run(query, f"load monthly cost of {source}"), source)

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        query = self._client.rpc("add_source_cost", {"p_source": source, "p_cost": str(cost)})
        return _cost(self._run(query, f"add cost of {source}", idempotent=False), source)

    def add_audit_cost(
        self,
        audit_id: UUID,
        source: str,
        service: str,
        *,
        calls: int,
        cost: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        query = self._client.rpc(
            "add_audit_cost",
            {
                "p_audit_id": str(audit_id),
                "p_source": source,
                "p_service": service,
                "p_calls": calls,
                "p_cost": str(cost),
                "p_tokens_in": tokens_in,
                "p_tokens_out": tokens_out,
            },
        )
        self._run(query, f"add cost of {source} {service} to audit {audit_id}", idempotent=False)

    def audit_costs(self, audit_id: UUID) -> list[AuditCost]:
        query = (
            self._client.table("audit_costs")
            .select("*")
            .eq("audit_id", str(audit_id))
            .order("source")
            .order("service")
        )
        rows = _rows(self._run(query, f"load costs of audit {audit_id}"))
        return [AuditCost.model_validate(row) for row in rows]

    def estimates(self, audit_id: UUID) -> list[AuditEstimate]:
        query = self._client.table("audit_estimates").select("*").eq("audit_id", str(audit_id))
        rows = _rows(self._run(query, f"load estimate of audit {audit_id}"))
        return [AuditEstimate.model_validate(row) for row in rows]

    def save_estimate(
        self, audit_id: UUID, lines: list[AuditEstimate], status: str, note: str
    ) -> None:
        rows: list[dict[str, Any]] = [
            {
                "audit_id": str(audit_id),
                "source": line.source,
                "service": line.service,
                "calls": line.calls,
                "unit_cost": str(line.unit_cost),
                "cost": str(line.cost),
            }
            for line in lines
        ]
        self._run(
            self._client.table("audit_estimates").upsert(rows), f"save estimate of audit {audit_id}"
        )
        values = {"estimate_status": status, "estimate_note": note}
        query = self._client.table("audits").update(values).eq("id", str(audit_id))
        self._run(query, f"save estimate status of audit {audit_id}")

    def set_setting(self, key: str, value: object) -> None:
        row: dict[str, Any] = {"key": key, "value": value}
        query = self._client.table("settings").upsert(row)
        self._run(query, f"save setting {key}")

    def customer_access(self, customer_id: UUID) -> CustomerAccess | None:
        query = (
            self._client.table("customer_access")
            .select(CUSTOMER_ACCESS_COLUMNS)
            .eq("customer_id", str(customer_id))
            .limit(1)
        )
        rows = _rows(self._run(query, f"load access of customer {customer_id}"))
        return CustomerAccess.model_validate(rows[0]) if rows else None

    def wordpress_password(self, customer_id: UUID) -> SecretStr | None:
        query = (
            self._client.table("customer_access")
            .select(WORDPRESS_PASSWORD_COLUMN)
            .eq("customer_id", str(customer_id))
            .limit(1)
        )
        rows = _rows(self._run(query, f"load WordPress access of customer {customer_id}"))
        value = rows[0].get(WORDPRESS_PASSWORD_COLUMN) if rows else None
        return SecretStr(value) if isinstance(value, str) and value.strip() else None

    def update_customer(
        self,
        customer_id: UUID,
        *,
        name: str | None = None,
        city: str | None = None,
        industry: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            key: value
            for key, value in (("name", name), ("city", city), ("industry", industry))
            if value is not None
        }
        if not values:
            return
        query = self._client.table("customers").update(values).eq("id", str(customer_id))
        self._run(query, f"update customer {customer_id}")

    def link_crm_customer(self, customer_id: UUID) -> UUID | None:
        query = self._client.rpc("link_crm_customer", {"p_customer_id": str(customer_id)})
        answer: object = self._run(query, f"link customer {customer_id} to the CRM").data
        if isinstance(answer, list):
            answer = answer[0] if answer else None
        if answer is None:
            return None
        try:
            return UUID(str(answer))
        except ValueError as exc:
            raise StorageError(f"link customer {customer_id} returned {answer!r}") from exc

    def create_location(self, location: Location) -> Location:
        query = self._client.table("locations").insert(location.model_dump(mode="json"))
        action = f"create location of customer {location.customer_id}"
        rows = _rows(self._run(query, action, idempotent=False))
        if not rows:
            raise StorageError(f"{action} returned no row")
        return Location.model_validate(rows[0])

    def update_location(
        self,
        location_id: UUID,
        *,
        name: str | None = None,
        street: str | None = None,
        postal_code: str | None = None,
        city: str | None = None,
        phone: str | None = None,
        google_place_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        given: dict[str, object] = {
            "name": name,
            "street": street,
            "postal_code": postal_code,
            "city": city,
            "phone": phone,
            "google_place_id": google_place_id,
            "latitude": latitude,
            "longitude": longitude,
        }
        values: dict[str, Any] = {key: value for key, value in given.items() if value is not None}
        if not values:
            return
        query = self._client.table("locations").update(values).eq("id", str(location_id))
        self._run(query, f"update location {location_id}")

    def add_keywords(
        self,
        customer_id: UUID,
        phrases: list[str],
        city: str,
        *,
        first_primary: bool,
        scope: KeywordScope = KeywordScope.LOCAL,
    ) -> list[Keyword]:
        if not phrases:
            return []
        rows: list[dict[str, Any]] = [
            {
                "customer_id": str(customer_id),
                "phrase": phrase,
                "city": city,
                "scope": scope.value,
                "is_primary": first_primary and index == 0,
            }
            for index, phrase in enumerate(phrases)
        ]
        query = self._client.table("keywords").upsert(
            rows, on_conflict="customer_id,phrase,city", ignore_duplicates=True
        )
        inserted = _rows(self._run(query, f"add keywords of customer {customer_id}"))
        return [Keyword.model_validate(row) for row in inserted]

    def update_keywords_city(self, keyword_ids: Sequence[UUID], city: str) -> None:
        if not keyword_ids:
            return
        values: dict[str, Any] = {"city": city}
        ids = [str(keyword_id) for keyword_id in keyword_ids]
        query = self._client.table("keywords").update(values).in_("id", ids)
        self._run(query, f"update city of {len(ids)} keywords")

    def locations(self, customer_id: UUID) -> list[Location]:
        return [Location.model_validate(row) for row in self._by_customer("locations", customer_id)]

    def competitors(self, customer_id: UUID) -> list[Competitor]:
        rows = self._by_customer("competitors", customer_id)
        return [Competitor.model_validate(row) for row in rows]

    def keywords(self, customer_id: UUID) -> list[Keyword]:
        return [Keyword.model_validate(row) for row in self._by_customer("keywords", customer_id)]

    def update_keyword_volume(self, keyword_id: UUID, volume: int) -> None:
        values: dict[str, Any] = {"monthly_volume": volume}
        query = self._client.table("keywords").update(values).eq("id", str(keyword_id))
        self._run(query, f"update volume of keyword {keyword_id}")

    def audit(self, audit_id: UUID) -> Audit:
        row = self._one("audits", "id", str(audit_id), f"load audit {audit_id}")
        return Audit.model_validate(row)

    def create_audit(self, customer_id: UUID, kind: AuditKind) -> Audit:
        row = {"customer_id": str(customer_id), "kind": kind.value}
        query = self._client.table("audits").insert(row)
        rows = _rows(self._run(query, f"create audit for {customer_id}", idempotent=False))
        if not rows:
            raise StorageError(f"create audit for {customer_id} returned no row")
        return Audit.model_validate(rows[0])

    def set_audit_status(
        self, audit_id: UUID, status: AuditStatus, error: str | None = None
    ) -> None:
        values: dict[str, Any] = {"status": status.value, "error": error}
        if status in FINISHED_STATUSES:
            values["finished_at"] = _now().isoformat()
        query = self._client.table("audits").update(values).eq("id", str(audit_id))
        self._run(query, f"set status of audit {audit_id}")

    def update_audit(
        self,
        audit_id: UUID,
        *,
        raw_data_path: str | None = None,
        total_score: Decimal | None = None,
        report_path: str | None = None,
        social: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if social is not None:
            values["social"] = social
        if raw_data_path is not None:
            values["raw_data_path"] = raw_data_path
        if total_score is not None:
            values["total_score"] = str(total_score)
        if report_path is not None:
            values["report_path"] = report_path
        if not values:
            return
        query = self._client.table("audits").update(values).eq("id", str(audit_id))
        self._run(query, f"update audit {audit_id}")

    def set_total_score(self, audit_id: UUID, total_score: Decimal | None) -> None:
        values = {"total_score": None if total_score is None else str(total_score)}
        query = self._client.table("audits").update(values).eq("id", str(audit_id))
        self._run(query, f"set total score of audit {audit_id}")

    def audit_ids_with_status(self, status: AuditStatus) -> list[UUID]:
        query = (
            self._client.table("audits")
            .select("id")
            .eq("status", status.value)
            .order("requested_at")
        )
        rows = _rows(self._run(query, f"look up audits with status {status.value}"))
        return [UUID(str(row["id"])) for row in rows]

    def reopen_control(self, audit_id: UUID) -> None:
        values: dict[str, Any] = {
            "control_state": ControlState.OPEN.value,
            "control_attempt": 0,
            "control_last_error": None,
        }
        for name in ("collected", "data", "evaluation"):
            for part in ("ok", "reason", "evidence"):
                values[f"verdict_{name}_{part}"] = None
        query = self._client.table("check_results").update(values).eq("audit_id", str(audit_id))
        self._run(query, f"reopen control of audit {audit_id}")

    def previous_audit(self, audit: Audit) -> Audit | None:
        query = (
            self._client.table("audits")
            .select("*")
            .eq("customer_id", str(audit.customer_id))
            .eq("status", AuditStatus.REPORTED.value)
            .lt("requested_at", audit.requested_at.isoformat())
            .neq("id", str(audit.id))
            .order("requested_at", desc=True)
            .limit(1)
        )
        rows = _rows(self._run(query, f"load audit before {audit.id}"))
        return Audit.model_validate(rows[0]) if rows else None

    def audits_of_customer(self, customer_id: UUID) -> list[Audit]:
        query = (
            self._client.table("audits")
            .select("*")
            .eq("customer_id", str(customer_id))
            .order("requested_at")
        )
        rows = _rows(self._run(query, f"load audits of customer {customer_id}"))
        return [Audit.model_validate(row) for row in rows]

    def check_results(self, audit_id: UUID) -> list[CheckResult]:
        query = (
            self._client.table("check_results")
            .select("*")
            .eq("audit_id", str(audit_id))
            .order("check_id")
        )
        rows = _rows(self._run(query, f"load check results of audit {audit_id}"))
        return [CheckResult.model_validate(row) for row in rows]

    def findings_of_audit(self, audit_id: UUID) -> list[Finding]:
        query = (
            self._client.table("findings")
            .select("*")
            .eq("audit_id", str(audit_id))
            .order("check_id")
        )
        rows = _rows(self._run(query, f"load findings of audit {audit_id}"))
        return [Finding.model_validate(row) for row in rows]

    def save_check_results(self, results: list[CheckResult]) -> None:
        rows = [result.model_dump(mode="json", exclude_none=True) for result in results]
        self._replace_by_audit("check_results", rows)

    def save_control_attempt(
        self, audit_id: UUID, check_id: str, attempt: int, last_error: str | None
    ) -> None:
        values: dict[str, Any] = {"control_attempt": attempt, "control_last_error": last_error}
        self._update_check_result(audit_id, check_id, values, "save control attempt")

    def save_check_verdicts(
        self,
        audit_id: UUID,
        check_id: str,
        collected: Verdict,
        data: Verdict | None,
        evaluation: Verdict | None,
    ) -> None:
        try:
            self._save_check_verdicts(audit_id, check_id, collected, data, evaluation)
        except StorageError as error:
            cause = error.__cause__
            if isinstance(cause, APIError) and cause.code == CONSTRAINT_VIOLATION_CODE:
                raise MigrationMissingError(
                    f"Datenbank-Änderung 025 fehlt: die Datenbank nimmt eine Kontrolle ohne "
                    f"Daten- und Auswertungsurteil nicht an (check {check_id}, audit {audit_id})"
                ) from cause
            raise

    def _save_check_verdicts(
        self,
        audit_id: UUID,
        check_id: str,
        collected: Verdict,
        data: Verdict | None,
        evaluation: Verdict | None,
    ) -> None:
        values: dict[str, Any] = {
            "control_state": ControlState.DONE.value,
            "control_last_error": None,
        }
        for name, verdict in (("collected", collected), ("data", data), ("evaluation", evaluation)):
            values[f"verdict_{name}_ok"] = verdict.ok if verdict is not None else None
            values[f"verdict_{name}_reason"] = verdict.reason if verdict is not None else None
            values[f"verdict_{name}_evidence"] = verdict.evidence if verdict is not None else None
        self._update_check_result(audit_id, check_id, values, "save verdicts")

    def _update_check_result(
        self, audit_id: UUID, check_id: str, values: dict[str, Any], action: str
    ) -> None:
        query = (
            self._client.table("check_results")
            .update(values)
            .eq("audit_id", str(audit_id))
            .eq("check_id", check_id)
        )
        self._run(query, f"{action} of check {check_id} of audit {audit_id}")

    def rankings(self, audit_id: UUID) -> list[Ranking]:
        query = (
            self._client.table("rankings")
            .select("*")
            .eq("audit_id", str(audit_id))
            .order("keyword_id")
            .order("subject")
        )
        rows = _rows(self._run(query, f"load rankings of audit {audit_id}"))
        return [Ranking.model_validate(row) for row in rows]

    def save_rankings(self, rankings: list[Ranking]) -> None:
        if not rankings:
            return
        rows = [ranking.model_dump(mode="json") for ranking in rankings]
        self._run(self._client.table("rankings").upsert(rows), "save rankings")

    def add_audit_events(self, audit_id: UUID, events: list[AuditEvent]) -> None:
        if not events:
            return
        if any(event.kind == "check" for event in events):
            delete = (
                self._client.table("audit_events")
                .delete()
                .eq("audit_id", str(audit_id))
                .eq("kind", "check")
            )
            self._run(delete, f"clear check events of audit {audit_id}")
        rows = [event.to_row(audit_id) for event in events]
        self._run(
            self._client.table("audit_events").insert(rows), f"save audit events of {audit_id}"
        )

    def save_findings(self, findings: list[Finding]) -> None:
        self._replace_by_audit("findings", [f.model_dump(mode="json") for f in findings])

    def _replace_by_audit(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        audit_ids = sorted({str(row["audit_id"]) for row in rows})
        delete = self._client.table(table).delete().in_("audit_id", audit_ids)
        self._run(delete, f"clear {table} of audits {', '.join(audit_ids)}")
        insert = self._client.table(table).upsert(rows, on_conflict="audit_id,check_id")
        self._run(insert, f"save {table} of audits {', '.join(audit_ids)}")

    def claim_jobs(self, kind: JobKind, limit: int) -> list[Job]:
        query = self._client.rpc("claim_jobs", {"p_kind": kind.value, "p_limit": limit})
        response = self._run(query, f"claim {kind.value} jobs", idempotent=False)
        return [Job.model_validate(row) for row in _rows(response)]

    def finish_job(self, job_id: UUID, status: JobStatus, error: str | None = None) -> None:
        values = {"status": status.value, "error": error, "updated_at": _now().isoformat()}
        query = self._client.table("jobs").update(values).eq("id", str(job_id))
        self._run(query, f"finish job {job_id}")

    def requeue_job(self, job_id: UUID, error: str, *, run_after: datetime | None = None) -> None:
        values: dict[str, Any] = {
            "status": JobStatus.QUEUED.value,
            "error": error,
            "updated_at": _now().isoformat(),
        }
        if run_after is not None:
            values["run_after"] = run_after.isoformat()
        query = self._client.table("jobs").update(values).eq("id", str(job_id))
        self._run(query, f"requeue job {job_id}")

    def requeue_running_jobs(self, error: str) -> int:
        values: dict[str, Any] = {
            "status": JobStatus.QUEUED.value,
            "error": error,
            "updated_at": _now().isoformat(),
        }
        query = self._client.table("jobs").update(values).eq("status", JobStatus.RUNNING.value)
        return len(_rows(self._run(query, "requeue running jobs")))

    def due_audit_ids(self, kinds: Sequence[JobKind], now: datetime) -> list[UUID]:
        query = (
            self._client.table("jobs")
            .select("audit_id")
            .eq("status", JobStatus.QUEUED.value)
            .in_("kind", [kind.value for kind in kinds])
            .filter("audit_id", "not.is", "null")
            .or_(f"run_after.is.null,run_after.lte.{now.isoformat()}")
            .order("created_at")
        )
        rows = _rows(self._run(query, "look up due audit jobs"))
        return list(dict.fromkeys(UUID(str(row["audit_id"])) for row in rows))

    def next_job_due(self, kinds: Sequence[JobKind], now: datetime) -> datetime | None:
        query = (
            self._client.table("jobs")
            .select("run_after")
            .eq("status", JobStatus.QUEUED.value)
            .in_("kind", [kind.value for kind in kinds])
            .filter("audit_id", "not.is", "null")
            .gt("run_after", now.isoformat())
            .order("run_after")
            .limit(1)
        )
        rows = _rows(self._run(query, "look up the next deferred audit job"))
        return datetime.fromisoformat(str(rows[0]["run_after"])) if rows else None

    def claim_audit_job(self, audit_id: UUID, kinds: Sequence[JobKind]) -> Job | None:
        query = self._client.rpc(
            "claim_audit_job",
            {"p_audit_id": str(audit_id), "p_kinds": [kind.value for kind in kinds]},
        )
        response = self._run(query, f"claim a job of audit {audit_id}", idempotent=False)
        rows = _rows(response)
        return Job.model_validate(rows[0]) if rows else None

    def jobs_of_audit(self, audit_id: UUID) -> list[Job]:
        query = self._client.table("jobs").select("*").eq("audit_id", str(audit_id))
        rows = _rows(self._run(query.order("created_at"), f"load jobs of audit {audit_id}"))
        return [Job.model_validate(row) for row in rows]

    def requeue_running_jobs_of_audit(self, audit_id: UUID, error: str) -> int:
        values: dict[str, Any] = {
            "status": JobStatus.QUEUED.value,
            "error": error,
            "updated_at": _now().isoformat(),
        }
        query = (
            self._client.table("jobs")
            .update(values)
            .eq("status", JobStatus.RUNNING.value)
            .eq("audit_id", str(audit_id))
        )
        return len(_rows(self._run(query, f"requeue running jobs of audit {audit_id}")))

    def reset_failed_jobs(self, audit_id: UUID, kinds: Sequence[JobKind]) -> list[Job]:
        values: dict[str, Any] = {
            "status": JobStatus.QUEUED.value,
            "attempts": 0,
            "error": None,
            "run_after": None,
            "updated_at": _now().isoformat(),
        }
        query = (
            self._client.table("jobs")
            .update(values)
            .eq("audit_id", str(audit_id))
            .eq("status", JobStatus.FAILED.value)
            .in_("kind", [kind.value for kind in kinds])
        )
        rows = _rows(self._run(query, f"reset failed jobs of audit {audit_id}"))
        return [Job.model_validate(row) for row in rows]

    def audit_event_rows(self, audit_id: UUID) -> list[dict[str, Any]]:
        query = (
            self._client.table("audit_events")
            .select("*")
            .eq("audit_id", str(audit_id))
            .order("occurred_at")
        )
        return _rows(self._run(query, f"load events of audit {audit_id}"))

    def add_manager_event(
        self, audit_id: UUID, result: Result, label: str, reason: str | None
    ) -> None:
        row = {
            "audit_id": str(audit_id),
            "kind": "manager",
            "phase": "auswertung",
            "result": result.value,
            "label": label,
            "reason": reason,
        }
        query = self._client.table("audit_events").insert(row)
        self._run(query, f"save manager event of audit {audit_id}", idempotent=False)

    def add_repair_event(
        self, audit_id: UUID, result: Result, label: str, reason: str | None, route: str
    ) -> None:
        row = {
            "audit_id": str(audit_id),
            "kind": "repair",
            "phase": "messung",
            "result": result.value,
            "label": label,
            "reason": reason,
            "route": route,
        }
        query = self._client.table("audit_events").insert(row)
        self._run(query, f"save repair event of audit {audit_id}", idempotent=False)

    def enqueue(
        self,
        kind: JobKind,
        customer_id: UUID,
        audit_id: UUID | None,
        *,
        run_after: datetime | None = None,
    ) -> Job:
        existing = (
            self._client.table("jobs")
            .select("*")
            .eq("kind", kind.value)
            .eq("status", JobStatus.QUEUED.value)
            .eq("customer_id", str(customer_id))
        )
        if audit_id is None:
            existing = existing.is_("audit_id", "null")
        else:
            existing = existing.eq("audit_id", str(audit_id))
        rows = _rows(self._run(existing.limit(1), f"look up queued {kind.value} job"))
        if rows:
            return Job.model_validate(rows[0])
        row = {
            "kind": kind.value,
            "customer_id": str(customer_id),
            "audit_id": str(audit_id) if audit_id else None,
        }
        if run_after is not None:
            row["run_after"] = run_after.isoformat()
        query = self._client.table("jobs").insert(row)
        inserted = _rows(self._run(query, f"enqueue {kind.value} job", idempotent=False))
        if not inserted:
            raise StorageError(f"enqueue {kind.value} job returned no row")
        return Job.model_validate(inserted[0])

    def save_document(self, document: Document) -> Document:
        query = self._client.table("documents").insert(document.model_dump(mode="json"))
        rows = _rows(self._run(query, f"save document {document.id}", idempotent=False))
        if not rows:
            raise StorageError(f"save document {document.id} returned no row")
        return Document.model_validate(rows[0])

    def document(self, document_id: UUID) -> Document:
        row = self._one("documents", "id", str(document_id), f"load document {document_id}")
        return Document.model_validate(row)

    def pending_documents(self) -> list[Document]:
        query = (
            self._client.table("documents")
            .select("*")
            .is_("file_path", "null")
            .is_("data->>error", "null")
            .order("created_at")
            .limit(PAGE_SIZE)
        )
        rows = _rows(self._run(query, "load document requests"))
        return [Document.model_validate(row) for row in rows]

    def complete_document(self, document: Document) -> Document:
        values = document.model_dump(
            mode="json",
            include={
                "number",
                "issued_on",
                "net_amount",
                "vat_rate",
                "gross_amount",
                "data",
                "file_path",
            },
        )
        query = (
            self._client.table("documents")
            .update(values)
            .eq("id", str(document.id))
            .is_("file_path", "null")
        )
        rows = _rows(self._run(query, f"complete document {document.id}", idempotent=False))
        if not rows:
            raise StorageError(f"complete document {document.id}: no open request with this id")
        return Document.model_validate(rows[0])

    def set_document_data(self, document_id: UUID, data: dict[str, object]) -> None:
        values: dict[str, Any] = {"data": data}
        query = (
            self._client.table("documents")
            .update(values)
            .eq("id", str(document_id))
            .is_("file_path", "null")
        )
        self._run(query, f"update data of document {document_id}")

    def _crm_one(self, table: str, row_id: UUID, action: str) -> dict[str, Any]:
        query = self._crm().table(table).select("*").eq("id", str(row_id)).limit(1)
        rows = _rows(self._run(query, action))
        if not rows:
            raise StorageError(f"{action} failed: no row in crm.{table} with id={row_id}")
        return rows[0]

    def crm_customer(self, customer_id: UUID) -> CrmCustomer:
        action = f"load CRM customer {customer_id}"
        return CrmCustomer.model_validate(self._crm_one("customers", customer_id, action))

    def crm_quote(self, quote_id: UUID) -> CrmQuote:
        action = f"load CRM quote {quote_id}"
        return CrmQuote.model_validate(self._crm_one("quotes", quote_id, action))

    def crm_quote_items(self, quote_id: UUID) -> list[CrmQuoteItem]:
        query = (
            self._crm()
            .table("quote_items")
            .select("*")
            .eq("quote_id", str(quote_id))
            .order("position")
        )
        rows = _rows(self._run(query, f"load items of CRM quote {quote_id}"))
        return [CrmQuoteItem.model_validate(row) for row in rows]

    def crm_order(self, order_id: UUID) -> CrmOrder:
        return CrmOrder.model_validate(self._crm_one("orders", order_id, f"load order {order_id}"))

    def crm_products(self, product_ids: list[UUID]) -> list[CrmProduct]:
        if not product_ids:
            return []
        ids = sorted({str(product_id) for product_id in product_ids})
        query = self._crm().table("products").select("*").in_("id", ids)
        rows = _rows(self._run(query, f"load {len(ids)} CRM products"))
        return [CrmProduct.model_validate(row) for row in rows]

    def claim_mail_triage(self, limit: int, seconds: int) -> list[MailTriage | UnreadableMailError]:
        params: dict[str, Any] = {"p_limit": limit, "p_seconds": seconds}
        query = self._crm().rpc("claim_mail_triage", params)
        rows = _rows(self._run(query, "claim mails for triage", idempotent=False))
        return [_claimed_mail(row) for row in rows]

    def classify_mail_triage(self, mail_id: UUID, result: dict[str, object]) -> None:
        params: dict[str, Any] = {"p_id": str(mail_id), "p_result": result}
        query = self._crm().rpc("classify_mail_triage", params)
        self._run(query, f"classify mail {mail_id}", idempotent=False)

    def fail_mail_triage(self, mail_id: UUID, error: str) -> None:
        params: dict[str, Any] = {"p_id": str(mail_id), "p_error": error}
        query = self._crm().rpc("fail_mail_triage", params)
        self._run(query, f"record failure of mail {mail_id}", idempotent=False)

    def claim_assistant_request(self) -> AssistantRequest | None:
        params: dict[str, Any] = {}
        query = self._crm().rpc("claim_assistant_request", params)
        rows = _rows(self._run(query, "claim assistant request", idempotent=False))
        if not rows:
            return None
        try:
            return AssistantRequest.model_validate(rows[0])
        except ValidationError as exc:
            raise StorageError(f"claim assistant request failed: unexpected row: {exc}") from exc

    def orphan_recordings(self) -> list[str]:
        params: dict[str, Any] = {}
        query = self._crm().rpc("orphan_recordings", params)
        rows = _rows(self._run(query, "list recordings without a request"))
        return [str(row["path"]) for row in rows]

    def save_assistant_transcript(self, request_id: UUID, text: str) -> None:
        params: dict[str, Any] = {"p_request": str(request_id), "p_text": text}
        query = self._crm().rpc("save_assistant_transcript", params)
        self._run(query, f"save transcript of assistant request {request_id}", idempotent=False)

    def apply_assistant_intent(
        self, request_id: UUID, intent: dict[str, object]
    ) -> AssistantResult:
        params: dict[str, Any] = {"p_request": str(request_id), "p_intent": intent}
        query = self._crm().rpc("apply_assistant_intent", params)
        action = f"apply assistant intent of request {request_id}"
        response = self._run(query, action, idempotent=False)
        try:
            return AssistantResult.model_validate(response.data)
        except ValidationError as exc:
            raise StorageError(f"{action} failed: unexpected answer {response.data!r}") from exc

    def fail_assistant_request(self, request_id: UUID, error: str) -> None:
        params: dict[str, Any] = {"p_request": str(request_id), "p_error": error}
        query = self._crm().rpc("fail_assistant_request", params)
        self._run(query, f"record failure of assistant request {request_id}", idempotent=False)

    def claim_prospect_search(self) -> ProspectSearch | None:
        query = self._client.rpc("claim_prospect_search", {})
        rows = _rows(self._run(query, "claim customer search", idempotent=False))
        return ProspectSearch.model_validate(rows[0]) if rows else None

    def finish_prospect_search(
        self, search_id: UUID, results: list[dict[str, object]], note: str | None
    ) -> int:
        params: dict[str, Any] = {"p_search": str(search_id), "p_results": results, "p_note": note}
        query = self._client.rpc("finish_prospect_search", params)
        value: object = self._run(query, f"store customer search {search_id}").data
        return value if isinstance(value, int) else len(results)

    def fail_prospect_search(self, search_id: UUID, error: str) -> None:
        params = {"p_search": str(search_id), "p_error": error}
        query = self._client.rpc("fail_prospect_search", params)
        self._run(query, f"record failure of customer search {search_id}")

    def queue_monthly_service(self, audit_id: UUID) -> UUID | None:
        query = self._client.rpc("queue_monthly_service", {"p_audit": str(audit_id)})
        value: object = self._run(query, f"queue monthly service of audit {audit_id}").data
        return UUID(value) if isinstance(value, str) else None

    def optimization_changes(
        self, order_id: UUID, status: OptimizationChangeStatus | None = None
    ) -> list[OptimizationChange]:
        query = self._client.table("optimization_changes").select("*").eq("order_id", str(order_id))
        if status is not None:
            query = query.eq("status", status.value)
        rows = _rows(self._run(query.order("created_at"), f"load changes of order {order_id}"))
        return [OptimizationChange.model_validate(row) for row in rows]

    def replace_draft_changes(
        self, order_id: UUID, audit_id: UUID | None, changes: list[OptimizationChange]
    ) -> None:
        delete = (
            self._client.table("optimization_changes")
            .delete()
            .eq("order_id", str(order_id))
            .eq("status", OptimizationChangeStatus.DRAFT.value)
        )
        delete = (
            delete.is_("audit_id", "null")
            if audit_id is None
            else delete.eq("audit_id", str(audit_id))
        )
        self._run(delete, f"clear drafts of order {order_id}")
        if not changes:
            return
        rows = [
            change.model_dump(
                mode="json", exclude={"decided_at", "applied_at", "error"}, exclude_none=True
            )
            for change in changes
        ]
        insert = self._client.table("optimization_changes").insert(rows)
        self._run(insert, f"save {len(rows)} drafts of order {order_id}", idempotent=False)

    def finish_change(
        self, change_id: UUID, status: OptimizationChangeStatus, error: str | None = None
    ) -> None:
        values: dict[str, Any] = {"status": status.value, "error": error}
        if status is OptimizationChangeStatus.APPLIED:
            values["applied_at"] = _now().isoformat()
        query = (
            self._client.table("optimization_changes")
            .update(values)
            .eq("id", str(change_id))
            .eq("status", OptimizationChangeStatus.APPROVED.value)
        )
        self._run(query, f"record outcome of change {change_id}")

    def emit_event(self, event: Event) -> None:
        query = self._client.table("events").upsert(event.model_dump(mode="json"))
        self._run(query, f"emit event {event.kind}")

    def new_event(self, kind: str, payload: dict[str, object]) -> Event:
        event = Event(id=uuid4(), kind=kind, payload=payload, created_at=_now(), delivered_at=None)
        self.emit_event(event)
        return event

    def pending_events(self) -> list[Event]:
        query = (
            self._client.table("events")
            .select("*")
            .is_("delivered_at", "null")
            .order("created_at")
            .limit(PAGE_SIZE)
        )
        return [Event.model_validate(row) for row in _rows(self._run(query, "load events"))]

    def mark_event_delivered(self, event_id: UUID) -> None:
        values = {"delivered_at": _now().isoformat()}
        query = self._client.table("events").update(values).eq("id", str(event_id))
        self._run(query, f"mark event {event_id} delivered")

    def upload(self, bucket: str, remote_path: str, local_file: Path) -> str:
        content_type = mimetypes.guess_type(local_file.name)[0] or "application/octet-stream"
        options: FileOptions = {"content-type": content_type, "upsert": "true"}
        try:
            payload = local_file.read_bytes()
            self._upload_with_retry(bucket, remote_path, payload, options)
        except OSError as exc:
            raise StorageError(f"read {local_file} for upload failed: {exc}") from exc
        except CLIENT_ERRORS as exc:
            raise StorageError(f"upload to {bucket}/{remote_path} failed: {exc}") from exc
        logger.info("Uploaded %s to %s/%s", local_file.name, bucket, remote_path)
        return remote_path

    def upload_bytes(self, bucket: str, remote_path: str, payload: bytes) -> str:
        content_type = mimetypes.guess_type(remote_path)[0] or "application/octet-stream"
        options: FileOptions = {"content-type": content_type, "upsert": "true"}
        try:
            self._upload_with_retry(bucket, remote_path, payload, options)
        except CLIENT_ERRORS as exc:
            raise StorageError(f"upload to {bucket}/{remote_path} failed: {exc}") from exc
        return remote_path

    def list_paths(self, bucket: str, folder: str) -> list[str]:
        try:
            entries = self._client.storage.from_(bucket).list(folder, {"limit": LISTING_LIMIT})
        except CLIENT_ERRORS as exc:
            raise StorageError(f"listing {bucket}/{folder} failed: {exc}") from exc
        return sorted(f"{folder}/{entry['name']}" for entry in entries)

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        reraise=True,
    )
    def _upload_with_retry(
        self, bucket: str, remote_path: str, payload: bytes, options: FileOptions
    ) -> None:
        self._client.storage.from_(bucket).upload(remote_path, payload, options)

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        reraise=True,
    )
    def _download_with_retry(self, bucket: str, remote_path: str) -> bytes:
        return self._client.storage.from_(bucket).download(remote_path)

    def download_bytes(self, bucket: str, remote_path: str) -> bytes:
        try:
            return self._download_with_retry(bucket, remote_path)
        except CLIENT_ERRORS as exc:
            raise StorageError(f"download of {bucket}/{remote_path} failed: {exc}") from exc

    def download(self, bucket: str, remote_path: str, local_file: Path) -> Path:
        content = self.download_bytes(bucket, remote_path)
        partial = local_file.with_name(f".{local_file.name}.part")
        try:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(content)
            partial.replace(local_file)
        except OSError as exc:
            raise StorageError(f"write {local_file} failed: {exc}") from exc
        return local_file

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        reraise=True,
    )
    def _remove_with_retry(self, bucket: str, remote_path: str) -> None:
        self._client.storage.from_(bucket).remove([remote_path])

    def remove(self, bucket: str, remote_path: str) -> None:
        try:
            self._remove_with_retry(bucket, remote_path)
        except CLIENT_ERRORS as exc:
            raise StorageError(f"remove {bucket}/{remote_path} failed: {exc}") from exc
        logger.info("Removed %s/%s", bucket, remote_path)
