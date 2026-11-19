import { CATALOGUE_AREAS, type CatalogueArea, type CatalogueCheck } from "./catalogue";

export const AREA_ORDER: readonly string[] = ["google", "bing", "apple", "openai", "anthropic", "ads"];

export const NO_RATING_TAG = "ohne Wertung";

export const SELECTION_STORAGE_KEY = "radarauditor.disabledChecks";

export type AreaState = "all" | "none" | "some";

export interface Tag {
  label: string;
}

type SelectionStorage = Pick<Storage, "getItem" | "setItem">;

export function orderedAreas(areas: readonly CatalogueArea[] = CATALOGUE_AREAS): CatalogueArea[] {
  const rank = (area: CatalogueArea) => {
    const index = AREA_ORDER.indexOf(area.id);
    return index === -1 ? AREA_ORDER.length : index;
  };
  return areas.map((area, index) => ({ area, index })).sort((a, b) => rank(a.area) - rank(b.area) || a.index - b.index).map(({ area }) => area);
}

export function areaTags(area: CatalogueArea): Tag[] {
  const tags: Tag[] = [];
  const paid = area.checks.filter((check) => check.requiresPaid).length;
  if (paid > 0) tags.push({ label: `${String(paid)} ${paid === 1 ? "kostet" : "kosten"} Geld` });
  if (area.checks.some((check) => check.requiresAccess)) tags.push({ label: "braucht Kundenzugang" });
  return tags;
}

export function checkTags(check: CatalogueCheck): Tag[] {
  const tags: Tag[] = [];
  if (check.kind === "info") tags.push({ label: NO_RATING_TAG });
  if (check.requiresPaid) tags.push({ label: "kostet Geld" });
  return tags;
}

function allIds(areas: readonly CatalogueArea[]): string[] {
  return areas.flatMap((area) => area.checks.map((check) => check.id));
}

export function defaultDisabled(areas: readonly CatalogueArea[] = CATALOGUE_AREAS): string[] {
  return areas.flatMap((area) => area.checks.filter((check) => check.requiresAccess).map((check) => check.id)).sort();
}

export function setCheck(disabled: readonly string[], checkId: string, enabled: boolean): string[] {
  const next = new Set(disabled);
  if (enabled) next.delete(checkId);
  else next.add(checkId);
  return [...next].sort();
}

export function setArea(disabled: readonly string[], area: CatalogueArea, enabled: boolean): string[] {
  const next = new Set(disabled);
  for (const check of area.checks) {
    if (enabled) next.delete(check.id);
    else next.add(check.id);
  }
  return [...next].sort();
}

export function setAll(areas: readonly CatalogueArea[], enabled: boolean): string[] {
  return enabled ? [] : allIds(areas).sort();
}

export function areaState(disabled: readonly string[], area: CatalogueArea): AreaState {
  const off = area.checks.filter((check) => disabled.includes(check.id)).length;
  if (off === 0) return "all";
  return off === area.checks.length ? "none" : "some";
}

export function enabledCount(disabled: readonly string[], areas: readonly CatalogueArea[]): { enabled: number; total: number } {
  const ids = allIds(areas);
  const off = new Set(disabled);
  return { enabled: ids.filter((id) => !off.has(id)).length, total: ids.length };
}

export function loadSelection(storage: SelectionStorage | null, areas: readonly CatalogueArea[] = CATALOGUE_AREAS): string[] {
  try {
    const text = storage?.getItem(SELECTION_STORAGE_KEY) ?? null;
    if (text === null) return defaultDisabled(areas);
    const parsed: unknown = JSON.parse(text);
    if (!Array.isArray(parsed)) return defaultDisabled(areas);
    const known = new Set(allIds(areas));
    return [...new Set(parsed.filter((item): item is string => typeof item === "string" && known.has(item)))].sort();
  } catch {
    return defaultDisabled(areas);
  }
}

export function saveSelection(storage: SelectionStorage | null, disabled: readonly string[]): boolean {
  try {
    storage?.setItem(SELECTION_STORAGE_KEY, JSON.stringify(disabled));
    return storage !== null;
  } catch {
    return false;
  }
}

export function deviceStorage(): SelectionStorage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
