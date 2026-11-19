import type { AuditCost, AuditEstimate } from "../api/types";

export interface PaidLine { source: string; service: string; calls: number; unitPrice: number | null; total: number; }
export interface UsageLine { source: string; service: string; calls: number; tokensIn: number; tokensOut: number; }
export interface CostStatement { paid: PaidLine[]; total: number; usage: UsageLine[]; }
export interface PlannedUse { source: string; service: string; calls: number; }
export interface EstimateStatement { paid: PaidLine[]; total: number; planned: PlannedUse[]; }

const SUBSCRIPTION_SOURCE = "claude";

const SOURCE_LABELS: Record<string, string> = {
  apify: "Apify",
  dataforseo: "DataForSEO",
  places: "Google Places",
  chatgpt: "OpenAI-Schnittstelle (Antworten von ChatGPT)",
  prospect_search: "Kundensuche (Google Places)",
  claude: "Claude (im Abo enthalten)",
};

const SERVICE_LABELS: Record<string, string> = {
  google_search: "Google-Suche",
  google_images: "Google-Bilder",
  instagram: "Instagram",
  facebook: "Facebook",
  linkedin: "LinkedIn",
  x: "X",
  assistants: "Fragen an den KI-Assistenten",
  openai_chat_completions: "Fragen an ChatGPT, abgerechnet nach Verbrauch",
  analysis: "Auswertung der Befunde",
  "/v3/serp/google/organic/live/advanced": "Google-Suchergebnisse",
  "/v3/serp/google/ai_mode/task_post": "Fragen an den Google KI-Modus",
  "/v3/serp/google/organic/task_post": "Google-Suche, drei Ergebnisseiten je Suchbegriff",
  "/v3/serp/bing/organic/task_post": "Bing-Suche",
  "/v3/serp/google/maps/task_post": "Google Maps, Kartenplätze und Kartenraster",
  "/v3/business_data/google/my_business_info/task_post": "Google-Profile",
  "/v3/business_data/google/reviews/task_post": "Google-Bewertungen mit Antworten des Inhabers",
  "/v3/business_data/google/questions_and_answers/task_post": "Fragen und Antworten im Google-Profil",
  "/v3/ai_optimization/chat_gpt/llm_scraper/task_post": "Fragen an ChatGPT",
  "/v3/keywords_data/google_ads/search_volume/live": "Suchvolumen",
  "/v3/backlinks/summary/live": "Rückverweise, Übersicht",
  "/v3/backlinks/referring_domains/live": "Rückverweise, verweisende Seiten",
  "/v3/dataforseo_labs/google/ranked_keywords/live": "Platzierte Suchbegriffe",
  "/v3/dataforseo_labs/google/domain_intersection/live": "Suchbegriffe der Wettbewerber",
  "/v3/dataforseo_labs/google/historical_rank_overview/live": "Verlauf der Platzierungen",
  text_search_essentials_ids_only: "Ortssuche, nur Kennung",
  text_search_enterprise: "Ortssuche, Firmendaten",
  text_search_enterprise_atmosphere: "Ortssuche, Firmendaten mit Bewertungen",
  place_details_enterprise_atmosphere: "Firmenprofil mit Bewertungen",
  apple_maps: "Apple Karten",
  bing_maps: "Bing Karten",
  directory_search: "Suche in Branchen- und Bewertungsportalen",
};

export function estimateStatement(rows: readonly AuditEstimate[]): EstimateStatement {
  const paid: PaidLine[] = [];
  const planned: PlannedUse[] = [];
  for (const row of rows) {
    const source = SOURCE_LABELS[row.source] ?? row.source;
    const service = SERVICE_LABELS[row.service] ?? row.service;
    if (row.source === SUBSCRIPTION_SOURCE) planned.push({ source, service, calls: row.calls });
    else paid.push({ source, service, calls: row.calls, unitPrice: row.unit_cost, total: row.cost });
  }
  return { paid, total: paid.reduce((sum, line) => sum + line.total, 0), planned };
}

export function costStatement(rows: readonly AuditCost[]): CostStatement {
  const paid: PaidLine[] = [];
  const usage: UsageLine[] = [];
  for (const row of rows) {
    const source = SOURCE_LABELS[row.source] ?? row.source;
    const service = SERVICE_LABELS[row.service] ?? row.service;
    if (row.source === SUBSCRIPTION_SOURCE) {
      usage.push({ source, service, calls: row.calls, tokensIn: row.tokens_in, tokensOut: row.tokens_out });
    } else {
      paid.push({ source, service, calls: row.calls, unitPrice: row.calls > 0 ? row.cost / row.calls : null, total: row.cost });
    }
  }
  return { paid, total: paid.reduce((sum, line) => sum + line.total, 0), usage };
}