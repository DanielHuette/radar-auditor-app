import type { CheckOutcome, CheckResult, CheckVerdicts } from "../api/types";
import control from "../generated/control.json";
import { isInfoCheck } from "./catalogue";

export type BoxKey = "collected" | "data" | "evaluation";
export type BoxState = "open" | "ok" | "bad";

export interface VerdictBox {
  key: BoxKey;
  label: string;
  state: BoxState;
  reason: string | null;
  evidence: string | null;
}

interface RawVerdict {
  ok: boolean | null;
  reason: string | null;
  evidence: string | null;
}

export const BOX_LABELS: Record<BoxKey, string> = {
  collected: "Daten korrekt eingeholt",
  data: "Daten korrekt",
  evaluation: "Auswertung korrekt",
};

export const BOX_KEYS: readonly BoxKey[] = ["collected", "data", "evaluation"];

export const REASON_MISSING = "Kein Grund gespeichert.";

const JUDGED_OUTCOMES: ReadonlySet<CheckOutcome> = new Set(["passed", "partial", "failed"]);

const CONTROLLED_OUTCOMES: ReadonlySet<CheckOutcome> = new Set(["passed", "partial", "failed", "not_measurable"]);

const PLANNED_CHECKS: ReadonlySet<string> = new Set(control.plannedChecks);
const PLANNED_REASONS: ReadonlySet<string> = new Set(control.plannedReasons);

export function isPlannedNotMeasurable(check: CheckResult): boolean {
  if (check.outcome !== "not_measurable") return false;
  const reason = check.measured.reason;
  if (reason === control.invalidReason) return false;
  return PLANNED_CHECKS.has(check.check_id) || (typeof reason === "string" && PLANNED_REASONS.has(reason));
}

export function isFailedMeasurement(check: CheckResult): boolean {
  return check.outcome === "not_measurable" && !isPlannedNotMeasurable(check);
}

function text(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? null : trimmed;
}

function rawVerdict(check: CheckVerdicts, key: BoxKey): RawVerdict {
  switch (key) {
    case "collected":
      return { ok: check.verdict_collected_ok ?? null, reason: text(check.verdict_collected_reason), evidence: text(check.verdict_collected_evidence) };
    case "data":
      return { ok: check.verdict_data_ok ?? null, reason: text(check.verdict_data_reason), evidence: text(check.verdict_data_evidence) };
    case "evaluation":
      return { ok: check.verdict_evaluation_ok ?? null, reason: text(check.verdict_evaluation_reason), evidence: text(check.verdict_evaluation_evidence) };
  }
}

function boxOf(key: BoxKey, verdict: RawVerdict, collectedFailed: boolean): VerdictBox {
  const open: VerdictBox = { key, label: BOX_LABELS[key], state: "open", reason: null, evidence: null };
  if (verdict.ok === null || (collectedFailed && key !== "collected")) return open;
  if (verdict.ok) return { ...open, state: "ok", evidence: verdict.evidence };
  return { ...open, state: "bad", reason: verdict.reason ?? REASON_MISSING, evidence: verdict.evidence };
}

export function verdictBoxes(check: CheckVerdicts): VerdictBox[] {
  const collectedFailed = rawVerdict(check, "collected").ok === false;
  return BOX_KEYS.map((key) => boxOf(key, rawVerdict(check, key), collectedFailed));
}

export function isExcludedByControl(check: CheckVerdicts): boolean {
  return verdictBoxes(check).some((box) => box.state === "bad");
}

export function controlDone(check: CheckVerdicts): boolean {
  return (check.control_state ?? "open") === "done";
}

export function awaitsControl(check: CheckResult): boolean {
  return !controlDone(check) && CONTROLLED_OUTCOMES.has(check.outcome) && !isPlannedNotMeasurable(check);
}

export function countsInScore(check: CheckResult): boolean {
  return JUDGED_OUTCOMES.has(check.outcome) && !isInfoCheck(check.check_id) && !isExcludedByControl(check);
}

export function controlRunning(checks: readonly CheckResult[]): boolean {
  return checks.some(awaitsControl);
}
