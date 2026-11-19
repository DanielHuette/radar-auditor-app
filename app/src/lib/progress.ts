import type { AuditEvent, AuditStatus, EventResult, JobKind, JobStatus, ManagerEvent } from "../api/types";

export type ProgressStep = "waiting" | "collecting" | "scoring" | "analysing" | "reporting" | "done" | "halted" | "failed";

export const PROGRESS_STEPS: readonly ProgressStep[] = ["waiting", "collecting", "scoring", "analysing", "reporting", "done"];

export const PROGRESS_LABELS: Record<ProgressStep, string> = {
  waiting: "Wartet",
  collecting: "Sammelt",
  scoring: "Bewertet",
  analysing: "Wertet aus",
  reporting: "Bericht",
  done: "Fertig",
  halted: "Angehalten",
  failed: "Fehler",
};

export const PROGRESS_TONES: Record<ProgressStep, string> = {
  waiting: "",
  collecting: "tone-busy",
  scoring: "tone-busy",
  analysing: "tone-busy",
  reporting: "tone-busy",
  done: "tone-ok",
  halted: "tone-warn",
  failed: "tone-bad",
};

export interface ProgressAudit {
  status: AuditStatus;
  error: string | null;
}

export interface ProgressJob {
  kind: JobKind;
  status: JobStatus;
  error: string | null;
  created_at: string;
  run_after: string | null;
}

export interface AnalysisWait {
  reason: string;
  nextAttempt: string | null;
}

export interface Progress {
  step: ProgressStep;
  serpRetry: boolean;
  analysisWait: AnalysisWait | null;
  error: string | null;
}

const AUDIT_STEPS: Record<AuditStatus, ProgressStep> = {
  requested: "waiting",
  collecting: "collecting",
  collected: "scoring",
  scored: "analysing",
  controlling: "scoring",
  awaiting_analysis: "analysing",
  analysed: "reporting",
  reported: "done",
  failed: "failed",
  halted: "halted",
};

const JOB_STEPS: Partial<Record<JobKind, ProgressStep>> = {
  repair: "collecting",
  score: "scoring",
  analyse: "analysing",
  report: "reporting",
};

export function stepOfAuditStatus(status: AuditStatus): ProgressStep {
  return AUDIT_STEPS[status];
}

export function isFinished(step: ProgressStep): boolean {
  return step === "done" || step === "failed";
}

function stepOfJob(job: ProgressJob): ProgressStep {
  if (job.status === "done" || job.status === "failed") return "waiting";
  if (job.kind === "collect") return job.status === "running" ? "collecting" : "waiting";
  return JOB_STEPS[job.kind] ?? "waiting";
}

function newestPerKind(jobs: readonly ProgressJob[]): ProgressJob[] {
  const newest = new Map<JobKind, ProgressJob>();
  for (const job of jobs) {
    const known = newest.get(job.kind);
    if (known === undefined || job.created_at >= known.created_at) newest.set(job.kind, job);
  }
  return [...newest.values()];
}

function later(first: ProgressStep, second: ProgressStep): ProgressStep {
  return PROGRESS_STEPS.indexOf(second) > PROGRESS_STEPS.indexOf(first) ? second : first;
}

function analysisWaitOf(audit: ProgressAudit, current: readonly ProgressJob[]): AnalysisWait | null {
  if (audit.status !== "awaiting_analysis") return null;
  const analyse = current.find((job) => job.kind === "analyse");
  if (analyse?.status !== "queued" || analyse.error === null || analyse.error.trim() === "") return null;
  return { reason: analyse.error.trim(), nextAttempt: analyse.run_after };
}

export function auditProgress(audit: ProgressAudit, jobs: readonly ProgressJob[]): Progress {
  const current = newestPerKind(jobs);
  const serpRetry = current.some((job) => job.kind === "serp_retry" && (job.status === "queued" || job.status === "running"));
  const auditStep = stepOfAuditStatus(audit.status);
  if (auditStep === "failed") return { step: "failed", serpRetry: false, analysisWait: null, error: audit.error };
  if (auditStep === "done") return { step: "done", serpRetry, analysisWait: null, error: null };
  if (auditStep === "halted") return { step: "halted", serpRetry: false, analysisWait: null, error: audit.error };

  const failedJob = current.find((job) => job.status === "failed" && job.kind !== "serp_retry");
  if (failedJob !== undefined) {
    return { step: "failed", serpRetry: false, analysisWait: null, error: failedJob.error ?? audit.error };
  }

  const step = current.map(stepOfJob).reduce(later, auditStep);
  return { step, serpRetry, analysisWait: analysisWaitOf(audit, current), error: null };
}

export const MANAGER_VERDICT_LABEL = "Audit-Manager: Ergebnis";

export interface RouteLine {
  key: string;
  label: string;
  result: EventResult;
  reason: string | null;
  stage: number | null;
}

export const EVENT_RESULT_LABELS: Record<EventResult, string> = {
  gemessen: "gemessen",
  fehlgeschlagen: "fehlgeschlagen",
  ausgesetzt: "ausgesetzt",
  "Schritt mit Daniel": "braucht Daniel",
  offen: "offen",
  "neu gestartet": "neu gestartet",
  Warnung: "Warnung",
  läuft: "läuft",
  gültig: "gültig",
  ungültig: "ungültig",
};

export const EVENT_RESULT_TONES: Record<EventResult, string> = {
  gemessen: "tone-ok",
  fehlgeschlagen: "tone-bad",
  ausgesetzt: "tone-warn",
  "Schritt mit Daniel": "tone-warn",
  offen: "",
  "neu gestartet": "tone-busy",
  Warnung: "tone-warn",
  läuft: "tone-busy",
  gültig: "tone-ok",
  ungültig: "tone-bad",
};

export function managerEvents(events: readonly AuditEvent[]): ManagerEvent[] {
  return events.filter((event) => event.kind === "manager");
}

export interface RepairLine {
  key: string;
  time: string;
  label: string;
  result: EventResult;
  reason: string | null;
}

export function repairLines(events: readonly AuditEvent[]): RepairLine[] {
  return events
    .filter((event) => event.kind === "repair")
    .map((event, index) => {
      const reason = event.reason?.trim() ?? "";
      return { key: `${event.occurred_at}-${String(index)}`, time: event.occurred_at, label: event.label, result: event.result, reason: reason === "" ? null : reason };
    });
}

export function checkEventCount(events: readonly AuditEvent[]): number {
  return new Set(events.filter((event) => event.kind === "check").map((event) => event.check_id)).size;
}

function byStage(left: RouteLine, right: RouteLine): number {
  return (left.stage ?? Number.MAX_SAFE_INTEGER) - (right.stage ?? Number.MAX_SAFE_INTEGER);
}

export function routeLines(events: readonly AuditEvent[]): RouteLine[] {
  const newest = new Map<string, RouteLine>();
  for (const event of events) {
    if (event.kind !== "route") continue;
    const key = event.route ?? event.label;
    const reason = event.reason?.trim() ?? "";
    newest.set(key, { key, label: event.label, result: event.result, reason: reason === "" ? null : reason, stage: event.stage });
  }
  return [...newest.values()].sort(byStage);
}

export interface ManagerNote {
  tone: "ok" | "bad" | "plain";
  text: string;
}

const VERDICT_TEXTS: Record<string, { tone: ManagerNote["tone"]; prefix: string }> = {
  gemessen: { tone: "ok", prefix: "Audit-Manager: vollständig und plausibel" },
  ausgesetzt: { tone: "plain", prefix: "Audit-Manager: noch nicht vollständig" },
  fehlgeschlagen: { tone: "bad", prefix: "Audit-Manager: fehlgeschlagen" },
};

function reportsToDaniel(event: ManagerEvent): boolean {
  return event.label === MANAGER_VERDICT_LABEL || event.result === "fehlgeschlagen";
}

export function managerNote(events: readonly ManagerEvent[]): ManagerNote | null {
  const newest = events.filter(reportsToDaniel).at(-1);
  if (newest === undefined) return null;
  const reason = newest.reason?.trim() ?? "";
  if (newest.label !== MANAGER_VERDICT_LABEL) return { tone: "bad", text: `Audit-Manager meldet ein Problem: ${reason}` };
  const verdict = VERDICT_TEXTS[newest.result] ?? { tone: "plain", prefix: "Audit-Manager" };
  return { tone: verdict.tone, text: reason === "" ? verdict.prefix : `${verdict.prefix} – ${reason}` };
}

const clock = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" });
const dayMonth = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit" });

function sameDay(first: Date, second: Date): boolean {
  return first.toDateString() === second.toDateString();
}

export function formatNextAttempt(runAfter: string | null, now: Date): string | null {
  if (runAfter === null) return null;
  const at = new Date(runAfter);
  if (Number.isNaN(at.getTime())) return null;
  if (at.getTime() <= now.getTime()) return "gleich";
  const time = `um ${clock.format(at)} Uhr`;
  return sameDay(at, now) ? time : `am ${dayMonth.format(at)} ${time}`;
}

export function analysisWaitText(wait: AnalysisWait, now: Date): string {
  const next = formatNextAttempt(wait.nextAttempt, now);
  return next === null ? `Auswertung wartet: ${wait.reason}` : `Auswertung wartet: ${wait.reason} – nächster Versuch ${next}`;
}
