import type { AuditStatus, JobStatus } from "../api/types";
import { AUDIT_STATUS_LABELS, CONTROL_RUNNING_LABEL, JOB_STATUS_LABELS } from "../lib/labels";

type Status = AuditStatus | JobStatus;

const TONES: Record<Status, string> = {
  requested: "",
  collecting: "tone-busy",
  collected: "tone-busy",
  scored: "tone-busy",
  controlling: "tone-busy",
  awaiting_analysis: "tone-warn",
  analysed: "tone-busy",
  reported: "tone-ok",
  failed: "tone-bad",
  halted: "tone-warn",
  queued: "",
  running: "tone-busy",
  done: "tone-ok",
};

const CONTROLLED_STATUSES: ReadonlySet<Status> = new Set(["scored", "awaiting_analysis", "analysed", "reported"]);

function label(status: Status): string {
  return status in AUDIT_STATUS_LABELS
    ? AUDIT_STATUS_LABELS[status as AuditStatus]
    : JOB_STATUS_LABELS[status as JobStatus];
}

export function JobStatusBadge({ status, controlRunning = false }: { status: Status; controlRunning?: boolean }) {
  if (controlRunning && CONTROLLED_STATUSES.has(status)) return <span class="badge tone-busy">{CONTROL_RUNNING_LABEL}</span>;
  return <span class={`badge ${TONES[status]}`}>{label(status)}</span>;
}
