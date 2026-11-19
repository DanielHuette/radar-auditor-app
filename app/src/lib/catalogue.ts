import generated from "../generated/catalogue.json";

export type CatalogueKind = "kriterium" | "ergebnis" | "info";

export interface CatalogueCheck {
  id: string;
  area: string;
  name: string;
  why: string;
  provider: string;
  kind: CatalogueKind;
  source: string;
  measurement: string;
  steps: readonly string[];
  weight: number;
  requiresPaid: boolean;
  requiresAccess: boolean;
  customerQuestion: string;
  customerImpact: string;
}

export interface CatalogueArea {
  id: string;
  name: string;
  weight: number;
  checks: readonly CatalogueCheck[];
}

const KINDS: readonly CatalogueKind[] = ["kriterium", "ergebnis", "info"];

function kindOf(value: string): CatalogueKind {
  const kind = KINDS.find((known) => known === value);
  if (kind === undefined) throw new Error(`Unknown check kind "${value}" in the generated catalogue`);
  return kind;
}

export const CATALOGUE_AREAS: readonly CatalogueArea[] = generated.areas.map((area) => ({
  ...area,
  checks: area.checks.map((check) => ({ ...check, kind: kindOf(check.kind) })),
}));

export const AREA_NAMES: Readonly<Record<string, string>> = Object.fromEntries(
  CATALOGUE_AREAS.map((area) => [area.id, area.name]),
);

export const AREA_IDS: readonly string[] = CATALOGUE_AREAS.map((area) => area.id);

const CHECKS = new Map(CATALOGUE_AREAS.flatMap((area) => area.checks.map((check) => [check.id, check] as const)));
const CHECK_ORDER = new Map([...CHECKS.keys()].map((id, index) => [id, index]));

const RADAR_LABELS: Readonly<Record<string, string>> = {
  google: "Google",
  bing: "Bing",
  apple: "Apple",
  openai: "ChatGPT",
  anthropic: "Claude",
  ads: "Bezahlt",
};

export function catalogueCheck(checkId: string): CatalogueCheck | undefined {
  return CHECKS.get(checkId);
}

export function checkName(checkId: string): string {
  return CHECKS.get(checkId)?.name ?? `Prüfung ${checkId}`;
}

export function isInfoCheck(checkId: string): boolean {
  return CHECKS.get(checkId)?.kind === "info";
}

export function catalogueOrder(checkId: string): number {
  return CHECK_ORDER.get(checkId) ?? CHECKS.size;
}

export function radarLabel(areaId: string): string {
  return RADAR_LABELS[areaId] ?? AREA_NAMES[areaId] ?? `Bereich ${areaId}`;
}

export function scoredAreas(): CatalogueArea[] {
  return CATALOGUE_AREAS.filter((area) => area.weight > 0);
}
