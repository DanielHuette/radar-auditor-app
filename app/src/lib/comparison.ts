export interface ComparisonInput {
  readonly checkId: string;
  readonly measured: Readonly<Record<string, unknown>>;
}

interface Rival {
  readonly name: string;
  readonly mentioned: number;
  readonly answers: number;
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function rivalOf(value: unknown): Rival | null {
  if (typeof value !== "object" || value === null) return null;
  const { name, mentioned, answers } = value as Record<string, unknown>;
  if (typeof name !== "string" || !isNumber(mentioned) || !isNumber(answers)) return null;
  return { name, mentioned, answers };
}

function count(value: number): string {
  return String(value);
}

const NO_RIVALS = "Wettbewerberwerte liegen nicht vor";

function gridComparison(measured: Readonly<Record<string, unknown>>): string | null {
  const { points_in_top: inTop, points_total: total, top, competitor_values: rivals } = measured;
  if (!isNumber(inTop) || !isNumber(total)) return null;
  const own = `${count(inTop)} von ${count(total)} Rasterpunkten in den ersten ${count(isNumber(top) ? top : 3)}`;
  return rivals === null || rivals === undefined ? `${own}; ${NO_RIVALS}` : own;
}

export function comparisonText({ checkId, measured }: ComparisonInput): string | null {
  if (checkId === "j3") return gridComparison(measured);
  const { mentioned, answers, competitors } = measured;
  if (!isNumber(mentioned) || !isNumber(answers)) return null;
  const own = `${count(mentioned)} von ${count(answers)} Antworten nennen den Betrieb`;
  const best = Array.isArray(competitors) ? rivalOf(competitors[0]) : null;
  if (best === null) return `${own}; ${NO_RIVALS}`;
  return `${own}; stärkster Wettbewerber (${best.name}): ${count(best.mentioned)} von ${count(best.answers)}`;
}