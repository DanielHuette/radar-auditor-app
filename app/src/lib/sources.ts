import type { SourceUsage } from "../api/types";
import routes from "../generated/routes.json";
import { parseAmount } from "./format";

export const SOURCES_KEY = "sources";

export const SOURCE_IDS = [
  "google_search", "google_maps", "places", "prospect_search", "dataforseo", "apify", "google_ai", "chatgpt", "claude",
] as const;
export type SourceId = (typeof SOURCE_IDS)[number];

export interface SourceSetting {
  enabled: boolean;
  daily_cap?: number;
  monthly_cost_cap_eur?: number;
}
export type SourceSettings = Record<SourceId, SourceSetting>;

export interface SourceInfo {
  label: string;
  explanation: string;
}

const SOURCE_LABELS: Record<SourceId, string> = {
  google_search: "Google-Suche",
  google_maps: "Google Maps",
  places: "Google Places API",
  prospect_search: "Kundensuche",
  dataforseo: "DataForSEO",
  apify: "Apify",
  google_ai: "Google KI-Modus",
  chatgpt: "ChatGPT",
  claude: "Claude",
};

const PROSPECT_SEARCH_EXPLANATION =
  "Bezahlte Google-Schnittstelle für den Reiter Kunden, mit eigenen Limits, damit die Prüfungen nie zu kurz kommen. Je 20 Betriebe eine Abfrage.";

const WAY_LABELS: Record<string, string> = {
  serp: "Suchergebnisse bei Google",
  "serp.bing": "Suchergebnisse bei Bing",
  "serp.google_images": "Google Bilder",
  "directories.apple_maps": "Eintrag in Apple Karten",
  "directories.bing_maps": "Eintrag in Bing Karten",
  "assistants.chatgpt": "Antworten von ChatGPT",
  "assistants.google_ai": "Antworten des Google-KI-Modus",
  "assistants.claude": "Antworten von Claude",
};

const BROWSER_SOURCES: Record<string, SourceId> = {
  places: "google_maps",
  serp: "google_search",
  "serp.google_images": "google_search",
  "assistants.chatgpt": "chatgpt",
};
const ASSISTANT_SOURCES: Record<string, SourceId> = {
  "assistants.chatgpt": "chatgpt",
  "assistants.google_ai": "google_ai",
};
const GOOGLE_SEARCH_WAYS = new Set(["serp", "serp.google_images"]);
const PAID_SUPPLIER_SOURCES: Record<string, SourceId> = {
  places_api: "places",
  dataforseo: "dataforseo",
  apify: "apify",
  openai_api: "chatgpt",
};

const suppliers: Record<string, string> = routes.suppliers;

export function sourcesOfRoute(wayKey: string, supplier: string): SourceId[] {
  const browser = BROWSER_SOURCES[wayKey];
  const assistant = ASSISTANT_SOURCES[wayKey];
  switch (supplier) {
    case "places_api":
      return ["places"];
    case "claude_code":
      return ["claude"];
    case "openai_api":
      return ["chatgpt"];
    case "dataforseo":
      return assistant === undefined ? ["dataforseo"] : ["dataforseo", assistant];
    case "apify":
      return GOOGLE_SEARCH_WAYS.has(wayKey) ? ["apify", "google_search"] : ["apify"];
    case "browser":
      return browser === undefined ? [] : [browser];
    default:
      return [];
  }
}

function builtRoutes(wayKey: string): string[] {
  const way = routes.ways.find((item) => item.key === wayKey);
  return way === undefined ? [] : way.routes.filter((route) => route.active).map((route) => route.supplier);
}

interface SourceUse {
  way: string;
  rank: number;
  of: number;
  supplier: string;
}

function usesOf(id: SourceId): SourceUse[] {
  return routes.ways.flatMap((way) => {
    const built = builtRoutes(way.key);
    return built.flatMap((supplier, index) =>
      sourcesOfRoute(way.key, supplier).includes(id)
        ? [{ way: WAY_LABELS[way.key] ?? way.label, rank: index + 1, of: built.length, supplier }]
        : [],
    );
  });
}

function supplierLabel(supplier: string): string {
  return suppliers[supplier] ?? supplier;
}

const ORDINALS = ["", "erster", "zweiter", "dritter", "vierter", "fünfter"];
const COUNTS = ["", "einem", "zwei", "drei", "vier", "fünf"];
const VIA: Record<string, string> = {
  browser: "den Browser auf dem Server",
  places_api: "die Google Places API",
  openai_api: "die OpenAI-Schnittstelle",
};
const SUPPLIER_NOTES: Record<string, string> = {
  places_api: "Die Google Places API ist die amtliche Schnittstelle von Google für Unternehmensprofile.",
  openai_api: "Die OpenAI-Schnittstelle ist der Programmzugang von OpenAI, dem Hersteller von ChatGPT.",
};

function sentence(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

function position(use: SourceUse): string {
  if (use.of === 1) return `einziger Weg für ${use.way}`;
  const place = use.rank === use.of ? "letzter" : (ORDINALS[use.rank] ?? `${String(use.rank)}.`);
  return `${place} von ${COUNTS[use.of] ?? String(use.of)} Wegen für ${use.way}`;
}

function costSentences(id: SourceId, supplier: string): string[] {
  const owner = PAID_SUPPLIER_SOURCES[supplier];
  if (owner === undefined) return ["Kostet nichts je Abfrage."];
  if (owner === id) return ["Kostet je Abfrage."];
  return ["Kostet je Abfrage.", `Die Kosten zählen zum Monatslimit von ${SOURCE_LABELS[owner]}.`];
}

function describeSupplier(id: SourceId, supplier: string, uses: readonly SourceUse[], alone: boolean): string[] {
  const where = alone
    ? uses.map((use) => sentence(position(use)))
    : [sentence(`Über ${VIA[supplier] ?? supplierLabel(supplier)}: ${uses.map(position).join(", ")}`)];
  const note = SUPPLIER_NOTES[supplier];
  return [...where, ...(note === undefined ? [] : [note]), ...costSentences(id, supplier)];
}

function explain(id: SourceId): string {
  if (id === "prospect_search") return PROSPECT_SEARCH_EXPLANATION;
  const bySupplier = new Map<string, SourceUse[]>();
  for (const use of usesOf(id)) bySupplier.set(use.supplier, [...(bySupplier.get(use.supplier) ?? []), use]);
  return [...bySupplier]
    .flatMap(([supplier, uses]) => describeSupplier(id, supplier, uses, bySupplier.size === 1))
    .join(" ");
}
export const SOURCE_INFO = Object.fromEntries(
  SOURCE_IDS.map((id) => [id, { label: SOURCE_LABELS[id], explanation: explain(id) }]),
) as Record<SourceId, SourceInfo>;

export const LISTED_SOURCE_IDS: readonly SourceId[] = SOURCE_IDS.filter((id) => SOURCE_INFO[id].explanation !== "");

const DATAFORSEO_MONTHLY_CAP_EUR = 50;
const APIFY_MONTHLY_CAP_EUR = 10;
const PLACES_MONTHLY_CAP_EUR = 25;
const CHATGPT_MONTHLY_CAP_EUR = 5;
const PROSPECT_SEARCH_MONTHLY_CAP_EUR = 10;

export const DEFAULT_SOURCES: SourceSettings = {
  google_search: { enabled: true, daily_cap: 300 },
  google_maps: { enabled: true, daily_cap: 500 },
  places: { enabled: true, daily_cap: 500, monthly_cost_cap_eur: PLACES_MONTHLY_CAP_EUR },
  prospect_search: { enabled: true, daily_cap: 15, monthly_cost_cap_eur: PROSPECT_SEARCH_MONTHLY_CAP_EUR },
  dataforseo: { enabled: true, daily_cap: 1000, monthly_cost_cap_eur: DATAFORSEO_MONTHLY_CAP_EUR },
  apify: { enabled: true, daily_cap: 1000, monthly_cost_cap_eur: APIFY_MONTHLY_CAP_EUR },
  google_ai: { enabled: true, daily_cap: 200 },
  chatgpt: { enabled: true, daily_cap: 200, monthly_cost_cap_eur: CHATGPT_MONTHLY_CAP_EUR },
  claude: { enabled: true, daily_cap: 200 },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function limit(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function parseSetting(value: unknown, fallback: SourceSetting): SourceSetting {
  if (!isRecord(value)) return fallback;
  const setting: SourceSetting = { enabled: typeof value.enabled === "boolean" ? value.enabled : fallback.enabled };
  const daily = limit(value.daily_cap) ?? fallback.daily_cap;
  const monthly = limit(value.monthly_cost_cap_eur) ?? fallback.monthly_cost_cap_eur;
  if (daily !== undefined) setting.daily_cap = daily;
  if (monthly !== undefined) setting.monthly_cost_cap_eur = monthly;
  return setting;
}

export function parseSources(value: unknown): SourceSettings {
  const stored = isRecord(value) ? value : {};
  return Object.fromEntries(
    SOURCE_IDS.map((id) => [id, parseSetting(stored[id], DEFAULT_SOURCES[id])]),
  ) as SourceSettings;
}

export function withSource(settings: SourceSettings, id: SourceId, change: Partial<SourceSetting>): SourceSettings {
  const next: SourceSetting = { ...settings[id], ...change };
  if (next.daily_cap === undefined) delete next.daily_cap;
  if (next.monthly_cost_cap_eur === undefined) delete next.monthly_cost_cap_eur;
  return { ...settings, [id]: next };
}

export type LimitInput = { value: number | undefined } | { error: string };

export function parseDailyCap(input: string): LimitInput {
  const text = input.trim();
  if (text === "") return { error: "Bitte ein Tageslimit als ganze Zahl eingeben." };
  return /^\d+$/.test(text) ? { value: Number(text) } : { error: "Bitte ein Tageslimit als ganze Zahl eingeben." };
}

export function costCapRequired(id: SourceId): boolean {
  return DEFAULT_SOURCES[id].monthly_cost_cap_eur !== undefined;
}

export function parseCostCap(input: string, required: boolean): LimitInput {
  if (input.trim() === "") {
    return required ? { error: "Für diese bezahlte Quelle ist ein Monatslimit Pflicht." } : { value: undefined };
  }
  const amount = parseAmount(input);
  return amount === null || amount < 0 ? { error: "Bitte einen Betrag wie 50 oder 49,50 eingeben." } : { value: amount };
}

export function monthStart(now: Date): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit" })
    .formatToParts(now);
  const year = parts.find((part) => part.type === "year")?.value ?? "1970";
  const month = parts.find((part) => part.type === "month")?.value ?? "01";
  return `${year}-${month}-01`;
}

export function monthUsage(rows: readonly SourceUsage[], fromDay: string): Partial<Record<string, number>> {
  const totals: Partial<Record<string, number>> = {};
  for (const row of rows) {
    if (row.day < fromDay) continue;
    totals[row.source] = (totals[row.source] ?? 0) + row.count;
  }
  return totals;
}
