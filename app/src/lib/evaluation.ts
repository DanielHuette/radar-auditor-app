import type { Audit, AuditEvent, CheckOutcome, CheckResult, Finding } from "../api/types";
import { AREA_IDS, AREA_NAMES, CATALOGUE_AREAS, catalogueOrder, checkName, isInfoCheck, radarLabel, scoredAreas, type CatalogueCheck } from "./catalogue";
import { invalidStepReasons, stepsOfCheck } from "./checkProgress";
import { isExcludedByControl } from "./checkVerdicts";
import { formatMeasured, reasonText } from "./measured";
import { areaScore } from "./scoring";

export type SortMode = "potential" | "area" | "urgency";

export const SORT_LABELS: Record<SortMode, string> = {
  potential: "Potenzial",
  area: "Bereich",
  urgency: "Dringlichkeit",
};

export interface CheckRow {
  check: CheckResult;
  finding: Finding | null;
  name: string;
  urgency: number;
  reason: string | null;
}

export interface AreaGroup {
  area: string;
  name: string;
  rows: CheckRow[];
  potential: number;
}

export interface PotentialTotals {
  points: number;
  openChecks: number;
  known: boolean;
}

const OPEN_OUTCOMES: ReadonlySet<CheckOutcome> = new Set(["failed", "partial"]);
const SCORED_STATUSES: ReadonlySet<Audit["status"]> = new Set(["scored", "awaiting_analysis", "analysed", "reported"]);

const points = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });

function amount(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function potentialOf(check: CheckResult): number {
  return amount(check.potential_points) ?? 0;
}

export function isOpen(check: CheckResult): boolean {
  return OPEN_OUTCOMES.has(check.outcome) && !isInfoCheck(check.check_id) && !isExcludedByControl(check);
}

export const REASON_NOT_STORED = "Kein Grund gespeichert.";
export const REASON_DESELECTED = "In dieser Prüfung abgewählt.";
export const REASON_NO_RESULT = "Kein Messergebnis gespeichert.";

function missingResultReason(check: CatalogueCheck): string {
  if (check.requiresAccess) return reasonText("no_access");
  return check.requiresPaid ? reasonText("no_paid_data") : REASON_NO_RESULT;
}

export const UNMEASURED_OUTCOMES: ReadonlySet<CheckOutcome> = new Set(["not_measurable", "skipped", "not_applicable"]);

export function checkEventReasons(events: readonly AuditEvent[]): Map<string, string> {
  const reasons = new Map(
    events.flatMap((event) => (event.kind === "check" && event.check_id !== null && event.reason !== null ? [[event.check_id, event.reason] as const] : [])),
  );
  const invalid = invalidStepReasons(events);
  for (const area of CATALOGUE_AREAS) {
    for (const entry of area.checks) {
      const step = stepsOfCheck(entry).find((name) => invalid.has(name));
      if (!reasons.has(entry.id) && step !== undefined) reasons.set(entry.id, invalid.get(step) ?? "");
    }
  }
  return reasons;
}

const NOT_MEASURED_PREFIX = /^(nicht gemessen:\s*)+/i;

function plainReason(reason: string): string {
  return reason.replace(NOT_MEASURED_PREFIX, "");
}

export function notMeasuredReason(check: CheckResult, eventReason: string | undefined): string | null {
  if (!UNMEASURED_OUTCOMES.has(check.outcome)) return null;
  const invalid = typeof check.measured.invalid_reason === "string" ? check.measured.invalid_reason.trim() : "";
  if (invalid !== "") return plainReason(invalid);
  const stored = typeof check.measured.reason === "string" ? check.measured.reason.trim() : "";
  if (stored !== "") return reasonText(stored);
  const fromEvent = eventReason?.trim() ?? "";
  return fromEvent === "" ? REASON_NOT_STORED : plainReason(fromEvent);
}

export function buildRows(
  checks: readonly CheckResult[],
  findings: readonly Finding[],
  eventReasons: ReadonlyMap<string, string> = new Map(),
): CheckRow[] {
  const byCheck = new Map(findings.map((finding) => [finding.check_id, finding]));
  return checks.map((check) => {
    const finding = byCheck.get(check.check_id) ?? null;
    return {
      check,
      finding,
      name: checkName(check.check_id),
      urgency: finding?.severity ?? 0,
      reason: notMeasuredReason(check, eventReasons.get(check.check_id)),
    };
  });
}

export interface UnmeasuredCheck {
  checkId: string;
  area: string;
  name: string;
  reason: string;
}

export function unmeasuredChecks(checks: readonly CheckResult[], disabled: readonly string[]): UnmeasuredCheck[] {
  const measured = new Set(checks.map((check) => check.check_id));
  return CATALOGUE_AREAS.flatMap((area) =>
    area.checks
      .filter((check) => !measured.has(check.id))
      .map((check) => ({
        checkId: check.id,
        area: area.id,
        name: check.name,
        reason: disabled.includes(check.id) ? REASON_DESELECTED : missingResultReason(check),
      })),
  );
}

export function orphanFindings(checks: readonly CheckResult[], findings: readonly Finding[]): Finding[] {
  const known = new Set(checks.map((check) => check.check_id));
  return findings.filter((finding) => !known.has(finding.check_id));
}

function byCatalogue(left: CheckRow, right: CheckRow): number {
  return catalogueOrder(left.check.check_id) - catalogueOrder(right.check.check_id)
    || left.check.check_id.localeCompare(right.check.check_id, "de", { numeric: true });
}

function byPotential(left: CheckRow, right: CheckRow): number {
  return potentialOf(right.check) - potentialOf(left.check) || right.urgency - left.urgency || byCatalogue(left, right);
}

function byUrgency(left: CheckRow, right: CheckRow): number {
  return right.urgency - left.urgency || potentialOf(right.check) - potentialOf(left.check) || byCatalogue(left, right);
}

const ROW_ORDER: Record<SortMode, (left: CheckRow, right: CheckRow) => number> = {
  potential: byPotential,
  area: byCatalogue,
  urgency: byUrgency,
};

export function sortRows(rows: readonly CheckRow[], mode: SortMode): CheckRow[] {
  return [...rows].sort(ROW_ORDER[mode]);
}

function areaRank(area: string): number {
  const index = AREA_IDS.indexOf(area);
  return index === -1 ? AREA_IDS.length : index;
}

export function groupRows(rows: readonly CheckRow[], mode: SortMode, onlyOpen: boolean): AreaGroup[] {
  const groups = new Map<string, CheckRow[]>();
  for (const row of sortRows(rows, mode)) {
    if (onlyOpen && !isOpen(row.check)) continue;
    const list = groups.get(row.check.area) ?? [];
    list.push(row);
    groups.set(row.check.area, list);
  }
  const result = [...groups].map(([area, list]) => ({
    area,
    name: AREA_NAMES[area] ?? `Bereich ${area}`,
    rows: list,
    potential: list.reduce((sum, row) => sum + (isOpen(row.check) ? potentialOf(row.check) : 0), 0),
  }));
  const urgency = (group: AreaGroup) => Math.max(0, ...group.rows.map((row) => row.urgency));
  const groupOrder: Record<SortMode, (left: AreaGroup, right: AreaGroup) => number> = {
    potential: (left, right) => right.potential - left.potential,
    area: () => 0,
    urgency: (left, right) => urgency(right) - urgency(left) || right.potential - left.potential,
  };
  return result.sort((left, right) => groupOrder[mode](left, right) || areaRank(left.area) - areaRank(right.area));
}

export interface Lever {
  checkId: string;
  name: string;
  measured: string | null;
  potential: number;
}

const LEVERS = 3;

export function topLevers(checks: readonly CheckResult[], findings: readonly Finding[] = [], count = LEVERS): Lever[] {
  const explained = new Map(findings.map((finding) => [finding.check_id, finding.explanation.trim()]));
  return checks
    .filter((check) => isOpen(check) && potentialOf(check) > 0)
    .sort((left, right) => potentialOf(right) - potentialOf(left) || catalogueOrder(left.check_id) - catalogueOrder(right.check_id))
    .slice(0, count)
    .map((check) => {
      const [line] = formatMeasured(check.measured);
      const sentence = explained.get(check.check_id);
      return {
        checkId: check.check_id,
        name: checkName(check.check_id),
        measured: sentence !== undefined && sentence !== "" ? sentence : line === undefined ? null : `${line.label}: ${line.value}`,
        potential: potentialOf(check),
      };
    });
}

export function formatLeverPoints(value: number): string {
  return `+${points.format(value)}`;
}

export function checkAnchor(checkId: string): string {
  return `pruefung-${checkId}`;
}

export function potentialTotals(checks: readonly CheckResult[]): PotentialTotals {
  const open = checks.filter(isOpen);
  const sum = (pick: (check: CheckResult) => number | null) =>
    open.reduce((total, check) => total + (pick(check) ?? 0), 0);
  return {
    points: sum((check) => amount(check.potential_points)),
    openChecks: open.length,
    known: checks.some((check) => amount(check.potential_points) !== null),
  };
}

export function formatPotential(value: number): string {
  return `+${points.format(value)} ${value === 1 ? "Punkt" : "Punkte"}`;
}

export function previousAudit(history: readonly Audit[], current: Audit): Audit | null {
  const earlier = history
    .filter((audit) => audit.id !== current.id && audit.requested_at < current.requested_at)
    .filter((audit) => SCORED_STATUSES.has(audit.status) && audit.total_score !== null)
    .sort((left, right) => right.requested_at.localeCompare(left.requested_at));
  return earlier[0] ?? null;
}

export interface RadarAxis {
  area: string;
  label: string;
  name: string;
  score: number | null;
  previous: number | null;
}

export function radarAxes(current: readonly CheckResult[], previous: readonly CheckResult[] | null): RadarAxis[] {
  const scoreIn = (checks: readonly CheckResult[], area: string) => areaScore(checks.filter((check) => check.area === area));
  return scoredAreas().map((area) => ({
    area: area.id,
    label: radarLabel(area.id),
    name: area.name,
    score: scoreIn(current, area.id),
    previous: previous === null ? null : scoreIn(previous, area.id),
  }));
}
