import type { AuditStatus, CheckResult } from "../api/types";
import type { CatalogueArea, CatalogueCheck } from "./catalogue";
import { checkProgress, stepsOfCheck, type CheckProgress, type StepState } from "./checkProgress";
import { isExcludedByControl, isPlannedNotMeasurable, verdictBoxes, type VerdictBox } from "./checkVerdicts";
import { notMeasuredReason, UNMEASURED_OUTCOMES } from "./evaluation";
import { INFO_LABEL, OUTCOME_LABELS } from "./labels";

export interface CheckLine {
  checkId: string;
  name: string;
  statusLabel: string | null;
  reason: string | null;
  progress: CheckProgress | null;
  boxes: VerdictBox[];
  excluded: boolean;
}

export interface LineContext {
  auditStatus: AuditStatus;
  states: ReadonlyMap<string, StepState>;
  eventReasons: ReadonlyMap<string, string>;
}

const PLANNED_PROGRESS: CheckProgress = { stage: "done", wait: null };

function statusLabelOf(entry: CatalogueCheck, result: CheckResult | null): string | null {
  if (result !== null && UNMEASURED_OUTCOMES.has(result.outcome)) return OUTCOME_LABELS[result.outcome];
  return entry.kind === "info" ? INFO_LABEL : null;
}

export function checkLine(entry: CatalogueCheck, result: CheckResult | null, context: LineContext): CheckLine {
  const base = { checkId: entry.id, name: entry.name, statusLabel: statusLabelOf(entry, result) };
  if (result === null) {
    const progress = checkProgress({ auditStatus: context.auditStatus, steps: stepsOfCheck(entry), states: context.states, result });
    return { ...base, reason: null, progress, boxes: verdictBoxes({}), excluded: false };
  }
  const unmeasured = UNMEASURED_OUTCOMES.has(result.outcome);
  const reason = notMeasuredReason(result, context.eventReasons.get(entry.id));
  if (isPlannedNotMeasurable(result)) return { ...base, reason, progress: PLANNED_PROGRESS, boxes: [], excluded: false };
  if (unmeasured && result.outcome !== "not_measurable") return { ...base, reason, progress: null, boxes: [], excluded: false };
  const progress = checkProgress({ auditStatus: context.auditStatus, steps: [], states: context.states, result });
  return { ...base, reason, progress, boxes: verdictBoxes(result), excluded: isExcludedByControl(result) };
}

export interface AreaLines {
  area: string;
  name: string;
  lines: CheckLine[];
}

export function areaLines(
  areas: readonly CatalogueArea[],
  results: readonly CheckResult[],
  disabled: readonly string[],
  context: LineContext,
): AreaLines[] {
  const byCheck = new Map(results.map((result) => [result.check_id, result]));
  return areas
    .map((area) => ({
      area: area.id,
      name: area.name,
      lines: area.checks.filter((entry) => !disabled.includes(entry.id)).map((entry) => checkLine(entry, byCheck.get(entry.id) ?? null, context)),
    }))
    .filter((group) => group.lines.length > 0);
}
