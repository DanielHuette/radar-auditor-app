from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl


class Customer(BaseModel):
    id: UUID
    name: str
    website: HttpUrl | None
    industry: str
    city: str
    created_at: datetime
    crm_customer_id: UUID | None = None


class Location(BaseModel):
    id: UUID
    customer_id: UUID
    name: str
    street: str
    postal_code: str
    city: str
    phone: str
    google_place_id: str | None
    latitude: float | None
    longitude: float | None


class Contact(BaseModel):
    id: UUID
    customer_id: UUID
    name: str
    role: str
    email: str | None
    phone: str | None


class Competitor(BaseModel):
    id: UUID
    customer_id: UUID
    name: str
    google_place_id: str | None
    website: HttpUrl | None


class KeywordScope(StrEnum):
    LOCAL = "local"
    NATIONWIDE = "nationwide"


class Keyword(BaseModel):
    id: UUID
    customer_id: UUID
    phrase: str
    city: str
    monthly_volume: int | None
    is_primary: bool
    scope: KeywordScope = KeywordScope.LOCAL

    @property
    def search_phrase(self) -> str:
        if self.scope is KeywordScope.LOCAL:
            return f"{self.phrase} {self.city}"
        return self.phrase


class AuditCost(BaseModel):
    audit_id: UUID
    source: str
    service: str
    calls: int
    cost: Decimal
    tokens_in: int
    tokens_out: int


class AuditEstimate(BaseModel):
    source: str
    service: str
    calls: int
    unit_cost: Decimal
    cost: Decimal


class CustomerAccess(BaseModel):
    customer_id: UUID
    google_business_profile_granted: bool
    search_console_property: str | None
    wordpress_url: str | None
    wordpress_user: str | None
    granted_at: datetime | None
    google_location_name: str | None = None
    analytics_property_id: str | None = None


class AuditKind(StrEnum):
    REMOTE = "remote"
    WITH_ACCESS = "with_access"


class AuditPurpose(StrEnum):
    SALES = "sales"
    MONTHLY = "monthly"


class AuditStatus(StrEnum):
    REQUESTED = "requested"
    COLLECTING = "collecting"
    COLLECTED = "collected"
    SCORED = "scored"
    CONTROLLING = "controlling"
    AWAITING_ANALYSIS = "awaiting_analysis"
    ANALYSED = "analysed"
    REPORTED = "reported"
    FAILED = "failed"
    HALTED = "halted"


class Audit(BaseModel):
    id: UUID
    customer_id: UUID
    kind: AuditKind
    status: AuditStatus
    requested_at: datetime
    finished_at: datetime | None
    total_score: Decimal | None
    raw_data_path: str | None
    report_path: str | None
    disabled_checks: list[str] = Field(default_factory=list)
    purpose: AuditPurpose = AuditPurpose.SALES


class CheckOutcome(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_MEASURABLE = "not_measurable"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"


class ControlState(StrEnum):
    OPEN = "open"
    DONE = "done"


class Verdict(BaseModel):
    ok: bool
    reason: str | None = None
    evidence: str | None = None


class CheckResult(BaseModel):
    id: UUID
    audit_id: UUID
    check_id: str
    area: str
    outcome: CheckOutcome
    measured: dict[str, object]
    weight: Decimal
    potential_points: Decimal | None = None
    uplift_low: Decimal | None = None
    uplift_high: Decimal | None = None
    provider: str | None = None
    control_state: ControlState = ControlState.OPEN
    control_attempt: int = 0
    control_last_error: str | None = None
    verdict_collected_ok: bool | None = None
    verdict_collected_reason: str | None = None
    verdict_collected_evidence: str | None = None
    verdict_data_ok: bool | None = None
    verdict_data_reason: str | None = None
    verdict_data_evidence: str | None = None
    verdict_evaluation_ok: bool | None = None
    verdict_evaluation_reason: str | None = None
    verdict_evaluation_evidence: str | None = None

    @property
    def verdict_flags(self) -> tuple[bool | None, bool | None, bool | None]:
        return (self.verdict_collected_ok, self.verdict_data_ok, self.verdict_evaluation_ok)

    @property
    def control_failed(self) -> bool:
        return any(flag is False for flag in self.verdict_flags)

    @property
    def verdicts_all_ok(self) -> bool:
        return all(flag is True for flag in self.verdict_flags)


class Finding(BaseModel):
    id: UUID
    audit_id: UUID
    check_id: str
    severity: int
    title: str
    explanation: str
    customer_impact: str
    fix: str
    fix_payload: dict[str, object] | None


class Ranking(BaseModel):
    id: UUID
    audit_id: UUID
    keyword_id: UUID
    subject: str
    organic_position: int | None
    local_pack_position: int | None
    grid_point: str | None
    measured_at: datetime


class JobKind(StrEnum):
    COLLECT = "collect"
    SCORE = "score"
    ANALYSE = "analyse"
    REPORT = "report"
    SERP_RETRY = "serp_retry"
    REPAIR = "repair"
    OPTIMIZE = "optimize"
    APPLY = "apply"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(BaseModel):
    id: UUID
    kind: JobKind
    status: JobStatus
    audit_id: UUID | None
    customer_id: UUID
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    order_id: UUID | None = None
    run_after: datetime | None = None


class DocumentKind(StrEnum):
    QUOTE = "quote"
    SERVICE_DESCRIPTION = "service_description"
    CONTRACT = "contract"
    PROOF_OF_SERVICE = "proof_of_service"
    AUDIT_QUOTE = "audit_quote"
    REVIEW_REPLIES = "review_replies"


class Document(BaseModel):
    id: UUID
    customer_id: UUID
    kind: DocumentKind
    number: str | None
    issued_on: date | None
    net_amount: Decimal | None
    vat_rate: Decimal | None
    gross_amount: Decimal | None
    data: dict[str, object]
    file_path: str | None


class Event(BaseModel):
    id: UUID
    kind: str
    payload: dict[str, object]
    created_at: datetime
    delivered_at: datetime | None


class OptimizationChangeStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    MANUAL = "manual"
    MANUAL_DONE = "manual_done"


class OptimizationChange(BaseModel):
    id: UUID
    order_id: UUID
    audit_id: UUID | None
    check_id: str | None
    target: str
    before: str | None
    after: str
    status: OptimizationChangeStatus
    error: str | None
    created_at: datetime
    decided_at: datetime | None
    applied_at: datetime | None


class ProspectSearchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


class ProspectSearch(BaseModel):
    id: UUID
    trade: str
    city: str
    status: ProspectSearchStatus
    error: str | None = None
    result_count: int | None = None
    requested_at: datetime
    claimed_at: datetime | None = None
    fetched_at: datetime | None = None
