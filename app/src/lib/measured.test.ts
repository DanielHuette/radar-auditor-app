import { describe, expect, it } from "vitest";
import { formatMeasured, joinListed, MEASURED_KEYS, REASON_CODES, reasonText, shortUrl, starsLabel, unansweredReviews } from "./measured";

const SCORER_SOURCES = import.meta.glob<string>(
  ["../../../src/radarauditor/scoring/checks/*.py", "../../../src/radarauditor/scoring/evaluator.py"],
  { query: "?raw", import: "default", eager: true },
);

const NOT_MEASURED = new Set([
  "name", "street", "phone", "address", "url", "type", "problem", "directories", "wikidata",
  "too_short", "too_long", "duplicate", "phone_or_email", "responsible",
  "places", "serp", "paid", "assistant_answers",
  "chatgpt", "wayback",
  "date", "stars", "text",
  "maps_result_list",
  "telephone", "page", "field", "value", "title", "main_heading", "image_description", "link_text",
  "title_or_heading_with_city", "text_with_city", "provider", "question", "excerpt",
  "customer_shown", "customer_position", "paid_entries", "ads_google", "ads_meta",
]);

function scorerSources(): string[] {
  return Object.values(SCORER_SOURCES);
}

function scorerKeys(): Set<string> {
  const keys = new Set<string>();
  for (const source of scorerSources()) {
    for (const match of source.matchAll(/"([a-z][a-z_]+)":/g)) keys.add(match[1] ?? "");
    for (const match of source.matchAll(/not_measurable\(\s*[^,)]+,\s*([a-z_]+)=/g)) keys.add(match[1] ?? "");
    for (const match of source.matchAll(/f"pages_\{key\}"/g)) keys.add(match[0]);
  }
  return keys;
}

function scorerReasons(): Set<string> {
  const reasons = new Set<string>();
  for (const source of scorerSources()) {
    for (const match of source.matchAll(/(?:not_measurable|_not_collected)\(\s*"([a-z_]+)"/g)) reasons.add(match[1] ?? "");
    for (const match of source.matchAll(/"reason": "([a-z_]+)"/g)) reasons.add(match[1] ?? "");
    for (const match of source.matchAll(/^NO_PAID_DATA = "([a-z_]+)"/gm)) reasons.add(match[1] ?? "");
  }
  return reasons;
}

describe("coverage of the scorer", () => {
  it("reads the scorer modules of the areas and the evaluator", () => {
    expect(Object.keys(SCORER_SOURCES).filter((path) => /\/[a-p]_\w+\.py$/.test(path)).length).toBeGreaterThanOrEqual(11);
    expect(Object.keys(SCORER_SOURCES).some((path) => path.endsWith("evaluator.py"))).toBe(true);
  });

  it("has a German label for every measured key the scorer writes", () => {
    const keys = [...scorerKeys()].filter((key) => !NOT_MEASURED.has(key));
    const lengthKeys = ["pages_missing", "pages_too_short", "pages_too_long", "pages_duplicate"];
    const expected = keys.flatMap((key) => (key === 'f"pages_{key}"' ? lengthKeys : [key]));
    expect(expected.filter((key) => !MEASURED_KEYS.includes(key))).toEqual([]);
  });

  it("explains every reason code the scorer writes", () => {
    expect([...scorerReasons()].filter((reason) => !REASON_CODES.includes(reason))).toEqual([]);
  });
});

describe("formatMeasured", () => {
  it("lists the rank changes against the previous audit per search phrase", () => {
    const lines = formatMeasured({
      previous_audit_id: "x",
      previous_requested_at: "2026-10-13T07:00:00Z",
      improved: 1,
      worsened: 0,
      unchanged: 2,
      changes: [
        { phrase: "künstlerin münster", kind: "organic", before: 12, after: 4 },
        { phrase: "atelier münster", kind: "local_pack", before: null, after: 3 },
      ],
    });
    expect(lines.map((line) => `${line.label}: ${line.value}`)).toEqual([
      "Verglichen mit der Prüfung vom: 13.10.2026",
      "Verbesserte Plätze: 1",
      "Verschlechterte Plätze: 0",
      "Unveränderte Plätze: 2",
      "Veränderungen je Suchbegriff: künstlerin münster (Suche): Platz 12 → Platz 4 · atelier münster (Kartenliste): nicht gefunden → Platz 3",
    ]);
  });

  it("turns page speed values into plain German with units", () => {
    const lines = formatMeasured({
      lcp_ms: 3120,
      lcp_source: "field_url",
      lcp_rating: "needs_improvement",
      cls: 0.14,
      cls_source: "field_origin",
      cls_rating: "needs_improvement",
      tbt_ms: 180,
      tbt_source: "lab",
      tbt_rating: "good",
      lab_runs: 5,
      mobile_performance: 54,
    });
    expect(lines.map((line) => [line.label, line.value])).toEqual([
      ["Hauptinhalt sichtbar nach", "3,12 Sekunden"],
      ["Hauptinhalt gemessen bei", "echten Besuchern dieser Seite (Google-Felddaten)"],
      ["Hauptinhalt bewertet als", "mittel"],
      ["Verrutschen beim Laden (CLS)", "0,14"],
      ["Verrutschen gemessen bei", "echten Besuchern der ganzen Website (Google-Felddaten)"],
      ["Verrutschen bewertet als", "mittel"],
      ["Blockierte Zeit beim Laden (TBT)", "180 Millisekunden"],
      ["Blockierte Zeit gemessen bei", "Google-Labormessung, mittlerer Wert der Läufe"],
      ["Blockierte Zeit bewertet als", "gut"],
      ["Labormessungen am Handy", "5"],
      ["Tempo-Wertung Handy", "54 von 100"],
    ]);
  });

  it("writes yes and no", () => {
    const lines = formatMeasured({ start_page_affected: true, robots_txt_present: false });
    expect(lines.map((line) => line.value)).toEqual(["ja", "nein"]);
  });

  it("shows pages as paths and shortens long lists", () => {
    const [line] = formatMeasured({
      pages_affected: ["https://www.muster.de/", "https://muster.de/alt/", "https://muster.de/a", "https://muster.de/b", "https://muster.de/c"],
    });
    expect(line?.value).toBe("muster.de, /alt, /a, /b und 1 weitere");
  });

  it("translates field names, shares and ratings", () => {
    const lines = formatMeasured({ mismatching_sources: ["phone", "postal_code"], similarity: 0.425, rating: 4.6 });
    expect(Object.fromEntries(lines.map((line) => [line.key, line.value]))).toEqual({
      mismatching_sources: "Telefon, Postleitzahl",
      similarity: "42,5 %",
      rating: "4,6 Sterne",
    });
  });

  it("lists positions per search phrase and marks missing ones", () => {
    const [line] = formatMeasured({ positions: { "Dachdecker Münster": 7, Dachsanierung: null } });
    expect(line?.value).toBe("Dachdecker Münster: Platz 7 · Dachsanierung: nicht gefunden");
  });

  it("leaves out the grid positions, which the map grid already shows", () => {
    expect(formatMeasured({ positions: { r1c1: 3, r1c2: null }, points_total: 2 }).map((line) => line.key)).toEqual(["points_total"]);
  });

  it("describes directory listings with their differences", () => {
    const [line] = formatMeasured({
      listings: { "Bing Places": { found: true, mismatches: ["phone"] }, "Apple Maps": { found: false, mismatches: [] } },
    });
    expect(line?.value).toBe("Bing Places: gefunden, Telefon weicht ab · Apple Maps: nicht gefunden");
  });

  it("formats nested structures of content, ranking and access checks", () => {
    const lines = formatMeasured({
      duplicate_titles: { Start: ["https://muster.de/a", "https://muster.de/b"] },
      top_gap_keywords: [{ phrase: "Flachdach", volume: 320, competitor_domain: "aasee.de", competitor_position: 2 }],
      totals: { clicks: 120, impressions: 4300, ctr: 0.028, position: 11.4 },
      change_percent: { CALL_CLICKS: 12.5, WEBSITE_CLICKS: -4 },
    });
    expect(Object.fromEntries(lines.map((line) => [line.key, line.value]))).toEqual({
      duplicate_titles: "„Start“: /a, /b",
      top_gap_keywords: "Flachdach (320 Suchen im Monat, aasee.de auf Platz 2)",
      change_percent: "Anrufe: +12,5 % · Website-Klicks: -4 %",
      totals: "Klicks: 120 · Einblendungen: 4.300 · Klickrate: 2,8 % · Platz im Schnitt: 11,4",
    });
  });

  it("puts the reason first and skips unknown keys and empty values", () => {
    const lines = formatMeasured({ pages_affected_count: 0, reason: "no_place", internal_debug: 3, start_page_h1: "", city: null });
    expect(lines.map((line) => [line.label, line.value])).toEqual([
      ["Grund", "Kein Google-Unternehmensprofil gefunden."],
      ["Anzahl betroffener Seiten", "0"],
    ]);
  });

  it("returns nothing for an empty measurement", () => {
    expect(formatMeasured({})).toEqual([]);
  });

  it("names a check still waiting for the audit agent", () => {
    const [line] = formatMeasured({ reason: "agent_judgement" });
    expect(line?.value).toBe("Der Audit-Agent beurteilt das beim Lesen der Seiten.");
  });
});

describe("photo age, website tools and the Maps comparison", () => {
  it("lists pages with their reason, age and shared titles", () => {
    const lines = formatMeasured({
      pages_affected_reasons: { "https://muster.de/a": "no_links", "https://muster.de/b": "" },
      pages_stale: { "https://muster.de/alt": 812 },
      pages_with_same_title: { Start: ["https://muster.de/a", "https://muster.de/b"] },
    });
    expect(lines.map((line) => line.value)).toEqual([
      "/a: Auf den Seiten wurden keine Links gefunden. · /b",
      "/alt: 812 Tage alt",
      "„Start“: /a, /b",
    ]);
  });

  it("states what each AI crawler may read", () => {
    const [line] = formatMeasured({
      agents: {
        "Claude-SearchBot": { site_blocked: true, pages_blocked_count: 0 },
        "Claude-User": { site_blocked: false, pages_blocked_count: 3 },
        GPTBot: { site_blocked: false, pages_blocked_count: 0 },
      },
    });
    expect(line?.value).toBe("Claude-SearchBot: ganze Website gesperrt · Claude-User: 3 Seiten gesperrt · GPTBot: erlaubt");
  });

  it("shows the hours until a robots.txt change takes effect", () => {
    expect(formatMeasured({ changes_take_effect_hours: 24 })[0]?.value).toBe("24 Stunden");
    expect(formatMeasured({ changes_take_effect_hours: 1 })[0]?.value).toBe("1 Stunde");
  });

  it("shows the peer ratings and the answer examples of the assistants", () => {
    const lines = formatMeasured({
      peers: { "Dachtechnik Aasee": 4.8, "Kröger": 4.1 },
      examples: [
        { provider: "chatgpt", question: "Wer deckt mein Dach?", mentioned: true, excerpt: "Dachtechnik Aasee" },
        { provider: "claude", question: "Wer deckt mein Dach?", mentioned: false, excerpt: "Kein Name" },
      ],
    });
    expect(lines.map((line) => line.value)).toEqual([
      "ChatGPT: „Dachtechnik Aasee“ (genannt) · Claude: „Kein Name“ (nicht genannt)",
      "Dachtechnik Aasee: 4,8 Sterne · Kröger: 4,1 Sterne",
    ]);
  });

  it("shows the ads per search phrase and the places of a phrase", () => {
    const lines = formatMeasured({
      phrases: {
        "dachdecker münster": { customer_shown: true, customer_position: 2, competitors: ["https://aasee.de/"], paid_entries: 3 },
        "flachdach": { customer_shown: false, customer_position: null, competitors: [], paid_entries: 0 },
      },
      by_phrase: { "dachdecker münster": { title: ["https://muster.de/"], text_with_city: [] }, flachdach: {} },
    });
    expect(lines.map((line) => line.value)).toEqual([
      "dachdecker münster: eigene Anzeige auf Platz 2, Wettbewerber aasee.de · flachdach: keine eigene Anzeige",
      "dachdecker münster: Titel: 1 · flachdach: nirgends",
    ]);
  });

  it("explains the Maps result list of g1, given as a number or as a list of businesses", () => {
    const counted = formatMeasured({
      compared_businesses: 7,
      comparison_source: "maps_result_list",
      phrase: "Dachdecker Münster",
      without_review_count: ["Bedachungen Kröger"],
    });
    expect(counted.map((line) => [line.label, line.value])).toEqual([
      ["Verglichene Betriebe", "7"],
      ["Verglichen mit", "allen Betrieben der Kartenliste bei Google Maps"],
      ["Suchbegriff der Kartenliste", "Dachdecker Münster"],
      ["Ohne lesbare Bewertungszahl", "Bedachungen Kröger"],
    ]);
    const [listedLine] = formatMeasured({
      compared_businesses: [
        { name: "Dachtechnik Aasee", review_count: 112 },
        { name: "Kröger", review_count: 1 },
        { name: "Ohne Zahl", review_count: null },
      ],
    });
    expect(listedLine?.value).toBe("Dachtechnik Aasee: 112 Bewertungen · Kröger: 1 Bewertung · Ohne Zahl");
    expect(formatMeasured({ comparison_source: "competitor_profiles" })[0]?.value).toBe(
      "den drei stärksten hinterlegten Wettbewerbern",
    );
  });

  it("explains the new reasons in plain German", () => {
    expect(reasonText("photos_not_read")).toBe("Fotos bei Google Maps nicht gelesen.");
    expect(reasonText("no_dated_photos")).toBe("Kein Foto mit Datum gefunden.");
    expect(reasonText("scripts_not_recorded")).toBe("Startseite wurde vor dieser Prüfung erfasst; neu prüfen.");
    expect(reasonText("no_listed_businesses")).toBe("Keine Betriebe in der Kartenliste gefunden.");
    expect(reasonText("no_review_counts")).toBe("Bewertungszahlen der Betriebe nicht lesbar.");
  });
});

describe("unansweredReviews", () => {
  it("keeps the unanswered examples out of the lines and returns them as cards", () => {
    const measured = {
      review_count: 4,
      unanswered_examples: [
        { date: "2026-10-02", stars: 2, text: " Termin wurde zweimal verschoben. " },
        { date: "vor 3 Wochen", stars: 5, text: "Sauber gearbeitet." },
        { date: null, stars: 1, text: "" },
        { date: null, stars: null, text: "" },
        "kaputt",
      ],
    };
    expect(formatMeasured(measured).map((line) => line.key)).toEqual(["review_count"]);
    expect(unansweredReviews(measured)).toEqual([
      { when: "02.10.2026", stars: 2, text: "Termin wurde zweimal verschoben." },
      { when: "vor 3 Wochen", stars: 5, text: "Sauber gearbeitet." },
      { when: null, stars: 1, text: "" },
    ]);
    expect(starsLabel(2)).toBe("2 von 5 Sternen");
    expect(starsLabel(4.5)).toBe("4,5 von 5 Sternen");
  });

  it("keeps an out-of-range star value out", () => {
    expect(unansweredReviews({ unanswered_examples: [{ date: "2026-01-05", stars: 7, text: "Gut" }] })).toEqual([
      { when: "05.01.2026", stars: null, text: "Gut" },
    ]);
  });

  it("returns no cards without examples", () => {
    expect(unansweredReviews({})).toEqual([]);
    expect(unansweredReviews({ unanswered_examples: "nein" })).toEqual([]);
  });
});

describe("daily caps", () => {
  it("names the exhausted sources like the settings page", () => {
    const lines = formatMeasured({ reason: "Tageslimit erreicht", sources: ["places", "google_maps"] });
    expect(lines.map((line) => line.value)).toEqual(["Tageslimit erreicht", "Google Places API, Google Maps"]);
  });
});

describe("assistant names", () => {
  it("names the Google AI mode in the shares per assistant and in the exhausted sources", () => {
    const shares = formatMeasured({ by_provider: { google_ai: 0.5, chatgpt: 0.25, claude: 0 } });
    expect(shares[0]?.value).toBe("Google KI-Modus: 50 % · ChatGPT: 25 % · Claude: 0 %");
    const capped = formatMeasured({ reason: "Tageslimit erreicht", sources: ["google_ai"] });
    expect(capped[1]?.value).toBe("Google KI-Modus");
  });
});

describe("reasonText", () => {
  it("keeps German text from the worker and hides unknown codes", () => {
    expect(reasonText("Tageslimit erreicht")).toBe("Tageslimit erreicht");
    expect(reasonText("some_new_code")).toBe("Nicht messbar.");
  });
});

describe("shortUrl", () => {
  it("handles telephone and mail links and bare domains", () => {
    expect(shortUrl("tel:0251%20987650")).toBe("0251 987650");
    expect(shortUrl("mailto:info@muster.de")).toBe("info@muster.de");
    expect(shortUrl("aasee.de")).toBe("aasee.de");
  });
});

describe("joinListed", () => {
  it("returns null for no items and joins short lists completely", () => {
    expect(joinListed([])).toBeNull();
    expect(joinListed(["a", "b"])).toBe("a, b");
  });
});
