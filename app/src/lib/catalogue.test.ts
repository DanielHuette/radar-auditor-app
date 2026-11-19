import { describe, expect, it } from "vitest";
import { AREA_IDS, AREA_NAMES, CATALOGUE_AREAS, catalogueCheck, catalogueOrder, checkName, isInfoCheck, radarLabel, scoredAreas } from "./catalogue";

const PROVIDERS = ["google", "bing", "apple", "openai", "anthropic", "ads"];
const REMOVED_AREAS = ["recht", "social", "werkzeuge", "firmendaten", "strukturierte_daten"];

describe("catalogue outline", () => {
  it("lists the providers in the agreed order", () => {
    expect(AREA_IDS).toEqual(PROVIDERS);
    expect(CATALOGUE_AREAS.map((area) => area.name)).toEqual(["Google", "Bing und Copilot", "Apple", "ChatGPT", "Claude", "Bezahlte Sichtbarkeit"]);
  });

  it("holds 62 checks with unique ids", () => {
    const checks = CATALOGUE_AREAS.flatMap((area) => area.checks);
    expect(checks).toHaveLength(62);
    expect(new Set(checks.map((item) => item.id)).size).toBe(62);
  });

  it("assigns every check to its area and names the provider", () => {
    for (const area of CATALOGUE_AREAS) {
      expect(area.checks.every((item) => item.area === area.id && item.provider !== "")).toBe(true);
    }
    expect(catalogueCheck("p2")?.provider).toBe("meta_ads");
    expect(catalogueCheck("p1")?.provider).toBe("google_ads");
  });

  it("has none of the removed areas", () => {
    for (const id of REMOVED_AREAS) expect(AREA_IDS).not.toContain(id);
  });

  it("gives every criterion an official source link and every other kind none required", () => {
    const checks = CATALOGUE_AREAS.flatMap((area) => area.checks);
    const criteria = checks.filter((item) => item.kind === "kriterium");
    expect(criteria.length).toBeGreaterThan(0);
    expect(criteria.every((item) => item.source.startsWith("https://"))).toBe(true);
  });

  it("marks the six checks of the paid area as information", () => {
    const ads = CATALOGUE_AREAS.find((area) => area.id === "ads");
    expect(ads?.checks.map((item) => item.kind)).toEqual(["info", "info", "info", "info", "info", "info"]);
  });
});

describe("catalogue lookups", () => {
  it("names checks and areas, with a fallback for unknown ids", () => {
    expect(checkName("a2")).toBe("Die Seite lädt am Handy schnell und ruhig");
    expect(checkName("x7")).toBe("Prüfung x7");
    expect(AREA_NAMES.openai).toBe("ChatGPT");
    expect(radarLabel("bing")).toBe("Bing");
    expect(radarLabel("anthropic")).toBe("Claude");
    expect(radarLabel("ads")).toBe("Bezahlt");
    expect(radarLabel("z")).toBe("Bereich z");
  });

  it("orders checks as in the catalogue and unknown ones last", () => {
    expect(catalogueOrder("a2")).toBeLessThan(catalogueOrder("a4"));
    expect(catalogueOrder("m2")).toBeLessThan(catalogueOrder("a14"));
    expect(catalogueOrder("x1")).toBeGreaterThan(catalogueOrder("p6"));
  });

  it("finds a check and tells information from rated checks", () => {
    expect(catalogueCheck("p1")?.kind).toBe("info");
    expect(isInfoCheck("p1")).toBe(true);
    expect(isInfoCheck("a2")).toBe(false);
    expect(isInfoCheck("x1")).toBe(false);
    expect(catalogueCheck("x1")).toBeUndefined();
  });

  it("leaves the weight-0 area out of the scored areas", () => {
    expect(scoredAreas().map((area) => area.id)).toEqual(["google", "bing", "apple", "openai", "anthropic"]);
  });
});
