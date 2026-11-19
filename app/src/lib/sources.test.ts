import { describe, expect, it } from "vitest";
import routes from "../generated/routes.json";
import {
  DEFAULT_SOURCES,
  LISTED_SOURCE_IDS,
  SOURCE_IDS,
  SOURCE_INFO,
  costCapRequired,
  monthStart,
  monthUsage,
  parseCostCap,
  parseDailyCap,
  parseSources,
  sourcesOfRoute,
  withSource,
} from "./sources";

const ROUTES_WITHOUT_SWITCH = [
  "crawler: website_crawler",
  "pagespeed: google_pagespeed",
  "serp.bing: browser",
  "access: google_oauth",
];

describe("parseSources", () => {
  it("falls back to the defaults when nothing is stored", () => {
    const settings = parseSources(null);
    expect(settings).toEqual(DEFAULT_SOURCES);
    expect(settings.places).toEqual({ enabled: true, daily_cap: 500, monthly_cost_cap_eur: 25 });
    expect(settings.prospect_search).toEqual({ enabled: true, daily_cap: 15, monthly_cost_cap_eur: 10 });
    expect(settings.dataforseo.monthly_cost_cap_eur).toBe(50);
    expect(settings.apify.monthly_cost_cap_eur).toBe(10);
    expect(settings.chatgpt).toEqual({ enabled: true, daily_cap: 200, monthly_cost_cap_eur: 5 });
    expect(SOURCE_IDS.filter((id) => !settings[id].enabled)).toEqual([]);
  });

  it("keeps stored switches and limits and drops unknown sources", () => {
    const settings = parseSources({ places: { enabled: true, daily_cap: 100 }, openai: { enabled: true } });
    expect(settings.places).toEqual({ enabled: true, daily_cap: 100, monthly_cost_cap_eur: 25 });
    expect(settings).not.toHaveProperty("openai");
    expect(settings.chatgpt).toEqual({ enabled: true, daily_cap: 200, monthly_cost_cap_eur: 5 });
  });

  it("replaces broken entries and negative limits", () => {
    const settings = parseSources({ chatgpt: "an", claude: { enabled: "ja", daily_cap: -3 } });
    expect(settings.chatgpt).toEqual({ enabled: true, daily_cap: 200, monthly_cost_cap_eur: 5 });
    expect(settings.claude).toEqual({ enabled: true, daily_cap: 200 });
  });

  it("keeps the default cost cap when the stored entry has none", () => {
    expect(parseSources({ dataforseo: { enabled: false } }).dataforseo).toEqual({
      enabled: false,
      daily_cap: 1000,
      monthly_cost_cap_eur: 50,
    });
    expect(parseSources({ chatgpt: { enabled: true, monthly_cost_cap_eur: null } }).chatgpt.monthly_cost_cap_eur).toBe(5);
    expect(parseSources({ chatgpt: { enabled: true, monthly_cost_cap_eur: 2 } }).chatgpt.monthly_cost_cap_eur).toBe(2);
  });

  it("keeps the default cost cap of a paid source when the stored cap is null", () => {
    const settings = parseSources({
      places: { enabled: true, daily_cap: 5, monthly_cost_cap_eur: null },
      prospect_search: { enabled: true, monthly_cost_cap_eur: null },
      dataforseo: { enabled: true, monthly_cost_cap_eur: "x" },
    });
    expect(settings.places).toEqual({ enabled: true, daily_cap: 5, monthly_cost_cap_eur: 25 });
    expect(settings.prospect_search.monthly_cost_cap_eur).toBe(10);
    expect(settings.dataforseo.monthly_cost_cap_eur).toBe(50);
  });

  it("uses the daily caps the worker enforces when none is stored", () => {
    const settings = parseSources({ google_search: { enabled: true } });
    expect(settings.google_search.daily_cap).toBe(300);
    expect(SOURCE_IDS.filter((id) => DEFAULT_SOURCES[id].daily_cap === undefined)).toEqual([]);
  });
});

describe("withSource", () => {
  it("changes one source and removes cleared limits", () => {
    const start = parseSources({ claude: { enabled: true, daily_cap: 20 } });
    const next = withSource(start, "claude", { daily_cap: undefined, enabled: false });
    expect(next.claude).toEqual({ enabled: false });
    expect(next.google_ai).toBe(start.google_ai);
    expect(JSON.stringify(next.claude)).toBe('{"enabled":false}');
  });
});

describe("limit inputs", () => {
  it("reads daily caps as whole numbers and requires one", () => {
    expect(parseDailyCap(" 40 ")).toEqual({ value: 40 });
    expect(parseDailyCap("")).toHaveProperty("error");
    expect(parseDailyCap("4,5")).toHaveProperty("error");
  });

  it("reads euro caps with German decimals and requires one for DataForSEO", () => {
    expect(parseCostCap("49,50", true)).toEqual({ value: 49.5 });
    expect(parseCostCap("", false)).toEqual({ value: undefined });
    expect(parseCostCap("", true)).toHaveProperty("error");
    expect(parseCostCap("-1", false)).toHaveProperty("error");
    expect(costCapRequired("dataforseo")).toBe(true);
    expect(costCapRequired("places")).toBe(true);
    expect(costCapRequired("prospect_search")).toBe(true);
    expect(costCapRequired("chatgpt")).toBe(true);
  });
});

describe("google_ai", () => {
  it("is an assistant source with the same defaults as chatgpt and no euro limit of its own", () => {
    expect(SOURCE_IDS).toContain("google_ai");
    expect(parseSources(null).google_ai).toEqual({ enabled: true, daily_cap: 200 });
    expect(SOURCE_INFO.google_ai.label).toBe("Google KI-Modus");
    expect(costCapRequired("google_ai")).toBe(false);
  });

  it("keeps a stored switch and cap", () => {
    expect(parseSources({ google_ai: { enabled: false, daily_cap: 7 } }).google_ai).toEqual({ enabled: false, daily_cap: 7 });
  });
});

describe("monthUsage", () => {
  it("sums the uses of the month per source", () => {
    const rows = [
      { day: "2026-09-30", source: "dataforseo", count: 9 },
      { day: "2026-10-01", source: "dataforseo", count: 3 },
      { day: "2026-10-12", source: "dataforseo", count: 4 },
      { day: "2026-10-12", source: "chatgpt", count: 2 },
    ];
    expect(monthUsage(rows, "2026-10-01")).toEqual({ dataforseo: 7, chatgpt: 2 });
  });

  it("starts the month in Berlin time", () => {
    expect(monthStart(new Date("2026-10-31T23:30:00Z"))).toBe("2026-11-01");
    expect(monthStart(new Date("2026-10-15T08:00:00Z"))).toBe("2026-10-01");
  });
});

describe("source texts", () => {
  const routesInUse = routes.ways.flatMap((way) =>
    way.routes.filter((route) => route.active).map((route) => ({ way: way.key, supplier: route.supplier })),
  );

  it("names every route in use that has no switch or limit in the program", () => {
    const uncovered = routesInUse
      .filter((route) => sourcesOfRoute(route.way, route.supplier).length === 0)
      .map((route) => `${route.way}: ${route.supplier}`);
    expect(uncovered).toEqual(ROUTES_WITHOUT_SWITCH);
  });

  it("lists all sources, each with a text built from the routes file", () => {
    expect(LISTED_SOURCE_IDS).toEqual(SOURCE_IDS);
    for (const id of SOURCE_IDS.filter((source) => source !== "prospect_search")) {
      expect(SOURCE_INFO[id].explanation).toMatch(/Weg/);
    }
  });

  it.each([
    [
      "places",
      "Erster von drei Wegen für Google-Profil und Karte. Die Google Places API ist die amtliche Schnittstelle von Google für Unternehmensprofile. Kostet je Abfrage.",
    ],
    ["google_maps", "Letzter von drei Wegen für Google-Profil und Karte. Kostet nichts je Abfrage."],
    ["claude", "Einziger Weg für Antworten von Claude. Kostet nichts je Abfrage."],
    [
      "google_ai",
      "Einziger Weg für Antworten des Google-KI-Modus. Kostet je Abfrage. Die Kosten zählen zum Monatslimit von DataForSEO.",
    ],
    [
      "chatgpt",
      "Über DataForSEO: erster von drei Wegen für Antworten von ChatGPT. Kostet je Abfrage. Die Kosten zählen zum Monatslimit von DataForSEO. Über den Browser auf dem Server: zweiter von drei Wegen für Antworten von ChatGPT. Kostet nichts je Abfrage. Über die OpenAI-Schnittstelle: letzter von drei Wegen für Antworten von ChatGPT. Die OpenAI-Schnittstelle ist der Programmzugang von OpenAI, dem Hersteller von ChatGPT. Kostet je Abfrage.",
    ],
    [
      "google_search",
      "Über Apify: zweiter von drei Wegen für Suchergebnisse bei Google, erster von zwei Wegen für Google Bilder. Kostet je Abfrage. Die Kosten zählen zum Monatslimit von Apify. Über den Browser auf dem Server: letzter von drei Wegen für Suchergebnisse bei Google, letzter von zwei Wegen für Google Bilder. Kostet nichts je Abfrage.",
    ],
    [
      "dataforseo",
      "Zweiter von drei Wegen für Google-Profil und Karte. Erster von drei Wegen für Suchergebnisse bei Google. Erster von drei Wegen für Antworten von ChatGPT. Einziger Weg für Antworten des Google-KI-Modus. Einziger Weg für Linkdaten und Vergleichsdaten. Einziger Weg für Anzeigen bei Google. Kostet je Abfrage.",
    ],
    [
      "apify",
      "Zweiter von drei Wegen für Suchergebnisse bei Google. Erster von zwei Wegen für Google Bilder. Einziger Weg für Eintrag in Apple Karten. Einziger Weg für Eintrag in Bing Karten. Kostet je Abfrage.",
    ],
  ] as const)("describes %s from the routes file", (id, text) => {
    expect(SOURCE_INFO[id].explanation).toBe(text);
  });
  it("leaves out routes that are not built or not confirmed", () => {
    const apify = SOURCE_INFO.apify.explanation;
    expect(apify).not.toContain("Facebook");
    expect(SOURCE_INFO.dataforseo.explanation).not.toContain("Bing");
    expect(Object.values(SOURCE_INFO).map((info) => info.explanation).join(" ")).not.toContain("Apple-Maps-Schnittstelle");
  });
});