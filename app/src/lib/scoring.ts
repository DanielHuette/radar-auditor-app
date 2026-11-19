import type { Audit, CheckOutcome, CheckResult, Finding } from "../api/types";
import limits from "../generated/ampel.json";
import { formatDate } from "./format";
import { AREA_IDS, AREA_NAMES, isInfoCheck } from "./catalogue";
import { countsInScore, isExcludedByControl } from "./checkVerdicts";

export type Tone = "ok" | "warn" | "bad" | "none";

export interface AreaSummary {
  area: string;
  name: string;
  score: number | null;
  tone: Tone;
  counts: Record<CheckOutcome, number>;
  counted: number;
  ratable: number;
}

const POINTS: Readonly<Partial<Record<CheckOutcome, number>>> = {
  passed: 1,
  partial: 0.5,
  failed: 0,
};

export const GOOD_FROM = limits.greenFrom;
export const FAIR_FROM = limits.yellowFrom;

export function scoreTone(score: number | null): Tone {
  if (score === null) return "none";
  if (score >= GOOD_FROM) return "ok";
  if (score >= FAIR_FROM) return "warn";
  return "bad";
}

function emptyCounts(): Record<CheckOutcome, number> {
  return { passed: 0, partial: 0, failed: 0, not_measurable: 0, skipped: 0, not_applicable: 0 };
}

export function areaScore(checks: readonly CheckResult[]): number | null {
  const scored = checks.flatMap((check) => {
    const value = POINTS[check.outcome];
    return countsInScore(check) && value !== undefined ? [{ weight: check.weight, value }] : [];
  });
  if (scored.length === 0) return null;
  const weightSum = scored.reduce((sum, entry) => sum + entry.weight, 0);
  const share =
    weightSum === 0
      ? scored.reduce((sum, entry) => sum + entry.value, 0) / scored.length
      : scored.reduce((sum, entry) => sum + entry.weight * entry.value, 0) / weightSum;
  return Math.round(share * 10000) / 100;
}

export interface Coverage {
  counted: number;
  ratable: number;
}

const UNRATED_OUTCOMES: ReadonlySet<CheckOutcome> = new Set(["skipped", "not_applicable"]);

export function coverageOf(checks: readonly CheckResult[]): Coverage {
  const rated = checks.filter((check) => !isInfoCheck(check.check_id) && !UNRATED_OUTCOMES.has(check.outcome));
  return { counted: rated.filter(countsInScore).length, ratable: rated.length };
}

export function groupByArea(checks: readonly CheckResult[]): AreaSummary[] {
  const byArea = new Map<string, CheckResult[]>();
  for (const check of checks.filter((entry) => !isInfoCheck(entry.check_id))) {
    const list = byArea.get(check.area) ?? [];
    list.push(check);
    byArea.set(check.area, list);
  }
  const known = AREA_IDS.filter((area) => byArea.has(area));
  const unknown = [...byArea.keys()].filter((area) => !AREA_IDS.includes(area)).sort();
  return [...known, ...unknown].map((area) => {
    const list = byArea.get(area) ?? [];
    const counts = emptyCounts();
    for (const check of list.filter((entry) => !isExcludedByControl(entry))) counts[check.outcome] += 1;
    const score = areaScore(list);
    return { area, name: AREA_NAMES[area] ?? `Bereich ${area}`, score, tone: scoreTone(score), counts, ...coverageOf(list) };
  });
}

export function sortFindings(findings: readonly Finding[]): Finding[] {
  return [...findings].sort(
    (left, right) =>
      right.severity - left.severity ||
      left.check_id.localeCompare(right.check_id, "de", { numeric: true }),
  );
}

export interface ScorePoint {
  label: string;
  score: number;
}

export function scoreHistory(audits: readonly Audit[]): ScorePoint[] {
  return audits
    .filter((audit): audit is Audit & { total_score: number } => audit.total_score !== null)
    .map((audit) => ({ at: audit.finished_at ?? audit.requested_at, score: audit.total_score }))
    .sort((left, right) => left.at.localeCompare(right.at))
    .map((point) => ({ label: formatDate(point.at), score: point.score }));
}
