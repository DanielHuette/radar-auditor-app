import type { AuditEvent, AuditStatus, CheckResult } from "../api/types";
import type { CatalogueCheck } from "./catalogue";
import { awaitsControl } from "./checkVerdicts";

export type BarStage = "waiting" | "fetching" | "validating" | "evaluating" | "control" | "done";
export type StepState = "fetching" | "validating" | "valid" | "invalid";

export interface ControlWait {
  attempt: number;
  error: string | null;
}

export interface CheckProgress {
  stage: BarStage;
  wait: ControlWait | null;
}

export interface ProgressInput {
  auditStatus: AuditStatus;
  steps: readonly string[];
  states: ReadonlyMap<string, StepState>;
  result: CheckResult | null;
}

export const BAR_STAGES: readonly BarStage[] = ["waiting", "fetching", "validating", "evaluating", "control", "done"];

export const BAR_LABELS: Record<BarStage, string> = {
  waiting: "wartet",
  fetching: "Daten werden geholt",
  validating: "Daten werden geprüft",
  evaluating: "wird ausgewertet",
  control: "Kontrolle",
  done: "fertig",
};

const MEASUREMENT_ROUTE_PREFIX = "messung:";

const EVALUATING_WITHOUT_STEPS: ReadonlySet<AuditStatus> = new Set(["collected", "scored"]);

export function stepsOfCheck(check: CatalogueCheck | undefined): readonly string[] {
  return check === undefined ? [] : check.steps;
}

function stepOf(event: AuditEvent): string | null {
  const route = event.route ?? "";
  if (event.kind !== "measurement" || !route.startsWith(MEASUREMENT_ROUTE_PREFIX)) return null;
  return route.slice(MEASUREMENT_ROUTE_PREFIX.length);
}

function stateOf(event: AuditEvent): StepState | null {
  switch (event.result) {
    case "gültig":
      return "valid";
    case "ungültig":
      return "invalid";
    case "läuft":
      return event.protocol?.stage === "pruefen" ? "validating" : "fetching";
    default:
      return null;
  }
}

export function stepStates(events: readonly AuditEvent[]): Map<string, StepState> {
  const states = new Map<string, StepState>();
  for (const event of events) {
    const step = stepOf(event);
    const state = stateOf(event);
    if (step !== null && state !== null) states.set(step, state);
  }
  return states;
}

export function invalidStepReasons(events: readonly AuditEvent[]): Map<string, string> {
  const reasons = new Map<string, string>();
  for (const event of events) {
    const step = stepOf(event);
    if (step === null || event.result === "läuft") continue;
    const reason = (event.reason ?? "").trim();
    if (event.result === "ungültig" && reason !== "") reasons.set(step, reason);
    else reasons.delete(step);
  }
  return reasons;
}

function stageWithoutResult({ auditStatus, steps, states }: ProgressInput): BarStage {
  if (steps.length === 0) return EVALUATING_WITHOUT_STEPS.has(auditStatus) ? "evaluating" : "waiting";
  const known = steps.map((step) => states.get(step));
  if (known.every((state) => state === "valid" || state === "invalid")) return "evaluating";
  if (known.includes("validating")) return "validating";
  if (known.includes("fetching")) return "fetching";
  return "waiting";
}

function controlWaitOf(result: CheckResult): ControlWait | null {
  const error = result.control_last_error?.trim() ?? "";
  const attempt = result.control_attempt ?? 0;
  if (attempt === 0 && error === "") return null;
  return { attempt: Math.max(attempt, 1), error: error === "" ? null : error };
}

export function checkProgress(input: ProgressInput): CheckProgress {
  const { result } = input;
  if (result === null) return { stage: stageWithoutResult(input), wait: null };
  if (!awaitsControl(result)) return { stage: "done", wait: null };
  return { stage: "control", wait: controlWaitOf(result) };
}

export function barPercent(stage: BarStage): number {
  return Math.round((BAR_STAGES.indexOf(stage) / (BAR_STAGES.length - 1)) * 100);
}

export function controlWaitText(wait: ControlWait): string {
  const head = `Kontrolle wartet – Versuch ${String(wait.attempt)}`;
  return wait.error === null ? head : `${head}: ${wait.error}`;
}
