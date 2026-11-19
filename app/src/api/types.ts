export type AuditKind = "remote" | "with_access";
export type AuditPurpose = "sales" | "monthly";
export type AuditStatus =
  | "requested" | "collecting" | "collected" | "scored" | "controlling"
  | "awaiting_analysis" | "analysed" | "reported" | "failed" | "halted";
export type CheckOutcome = "passed" | "partial" | "failed" | "not_measurable" | "skipped" | "not_applicable";
export type JobKind =
  | "collect" | "repair" | "score" | "analyse" | "report" | "serp_retry" | "optimize" | "apply";
export type JobStatus = "queued" | "running" | "done" | "failed";
export type DocumentKind =
  | "quote" | "service_description" | "contract" | "proof_of_service" | "audit_quote" | "review_replies";
export type OptimizationChangeStatus = "draft" | "approved" | "rejected" | "applied" | "failed" | "manual" | "manual_done";

export interface Customer { id: string; name: string; website: string | null; industry: string; city: string; order_value_eur: number | null; created_at: string; crm_customer_id: string | null; }
export type KeywordScope = "local" | "nationwide";
export interface Keyword { id: string; customer_id: string; phrase: string; city: string; monthly_volume: number | null; is_primary: boolean; scope: KeywordScope; }
export interface Audit { id: string; customer_id: string; kind: AuditKind; status: AuditStatus; requested_at: string; finished_at: string | null; total_score: number | null; report_path: string | null; error: string | null; raw_data_path?: string | null; disabled_checks?: string[]; purpose?: AuditPurpose; estimate_status?: EstimateStatus | null; estimate_note?: string | null; }
export type EstimateStatus = "covered" | "insufficient" | "unchecked";
export interface AuditEstimate { audit_id: string; source: string; service: string; calls: number; unit_cost: number; cost: number; }
export type ControlState = "open" | "done";
export interface CheckVerdicts {
  control_state?: ControlState;
  control_attempt?: number;
  control_last_error?: string | null;
  verdict_collected_ok?: boolean | null;
  verdict_collected_reason?: string | null;
  verdict_collected_evidence?: string | null;
  verdict_data_ok?: boolean | null;
  verdict_data_reason?: string | null;
  verdict_data_evidence?: string | null;
  verdict_evaluation_ok?: boolean | null;
  verdict_evaluation_reason?: string | null;
  verdict_evaluation_evidence?: string | null;
}
export interface CheckResult extends CheckVerdicts { id: string; audit_id: string; check_id: string; area: string; outcome: CheckOutcome; measured: Record<string, unknown>; weight: number; potential_points: number | null; uplift_low: number | null; uplift_high: number | null; provider?: string | null; }
export interface Finding { id: string; audit_id: string; check_id: string; severity: 1 | 2 | 3; title: string; explanation: string; customer_impact: string; fix: string; fix_payload: Record<string, unknown> | null; }
export interface AuditCost { audit_id: string; source: string; service: string; calls: number; cost: number; tokens_in: number; tokens_out: number; }
export interface Ranking { id: string; audit_id: string; keyword_id: string; subject: string; organic_position: number | null; local_pack_position: number | null; grid_point: string | null; measured_at: string; }
export interface Job { id: string; kind: JobKind; status: JobStatus; audit_id: string | null; customer_id: string; attempts: number; error: string | null; created_at: string; updated_at: string; order_id: string | null; run_after: string | null; }
export interface Doc { id: string; customer_id: string; kind: DocumentKind; number: string | null; issued_on: string | null; net_amount: number | null; vat_rate: number | null; gross_amount: number | null; data: Record<string, unknown>; file_path: string | null; created_at: string; }
export interface OptimizationChange { id: string; order_id: string; audit_id: string | null; check_id: string | null; target: string; before: string | null; after: string; status: OptimizationChangeStatus; error: string | null; created_at: string; decided_at: string | null; applied_at: string | null; }
export interface SourceUsage { day: string; source: string; count: number; }

export type ProspectStatus = "queued" | "running" | "done" | "failed" | "expired";
export type ContactStatus = "allowed" | "uncertain" | "forbidden";
export interface ContactWay { channel: string; label: string; status: ContactStatus; note: string; }
export interface ProspectSearch { id: string; trade: string; city: string; status: ProspectStatus; error: string | null; result_count: number | null; requested_at: string; claimed_at: string | null; fetched_at: string | null; }
export interface ProspectResult { id: string; search_id: string; rank: number; place_id: string; name: string; address: string; street: string; postal_code: string; city: string; phone: string | null; website: string | null; maps_url: string | null; rating: number | null; review_count: number; photo_count: number; has_opening_hours: boolean; score: number; signals: string[]; reasons: string; contact_ways: unknown; crm_customer_id: string | null; objected: boolean; }

export type CrmBilling = "once" | "monthly";
export type CrmQuoteStatus = "draft" | "sent" | "accepted" | "declined" | "expired";
export type CrmOrderStatus = "open" | "in_progress" | "done" | "invoiced";
export interface CrmProduct { id: string; name: string; description: string; unit: string; price_net: number; vat_rate: number; billing: CrmBilling; active: boolean; radar_areas: string[]; }
export interface CrmQuote { id: string; number: string; customer_id: string; status: CrmQuoteStatus; issue_date: string; valid_until: string; intro: string; outro: string; net: number; vat: number; gross: number; created_at: string; sent_at: string | null; decided_at: string | null; }
export interface CrmQuoteItem { id: string; quote_id: string; position: number; product_id: string | null; title: string; description: string; quantity: number; unit: string; unit_price_net: number; vat_rate: number; }
export interface CrmOrder { id: string; number: string; customer_id: string; quote_id: string | null; title: string; status: CrmOrderStatus; due_date: string | null; created_at: string; }

export interface CustomerRef { name: string }
export type AuditWithCustomer = Audit & { customers: CustomerRef | null };

export type EventResult = "gemessen" | "fehlgeschlagen" | "ausgesetzt" | "Schritt mit Daniel" | "offen" | "neu gestartet" | "Warnung" | "läuft" | "gültig" | "ungültig";
export type EventKind = "route" | "check" | "manager" | "repair" | "measurement";
export interface ManagerEvent { occurred_at: string; result: EventResult; label: string; reason: string | null; }
export interface AuditEvent extends ManagerEvent { kind: EventKind; stage: number | null; route: string | null; check_id: string | null; protocol?: Record<string, unknown> | null; }

export interface OpenAuditRequest { id: string; customer_id: string | null; company: string; contact: string; website: string | null; kind: string; wished_at: string | null; status: string; created_at: string; }
