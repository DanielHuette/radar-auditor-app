import { describe, expect, it } from "vitest";
import { paidRouteText } from "./routes";

describe("paidRouteText", () => {
  it.each([
    [{ route: "dataforseo" }, "über DataForSEO"],
    [{ route: "apify" }, "über Apify"],
    [{ route: "places" }, "über Google-Schnittstelle"],
    [{ route: "OpenAI " }, "über OpenAI-Schnittstelle"],
    [{ routes: ["browser", "dataforseo"] }, "über DataForSEO"],
    [{ routes: [{ route: "places_api" }, { route: "browser" }] }, "über Google-Schnittstelle"],
    [{ routes: { chatgpt: "openai", google_ai: "browser" } }, "über OpenAI-Schnittstelle"],
    [{ route: "dataforseo", routes: ["places", "openai_api", "dataforseo"] }, "über DataForSEO, Google-Schnittstelle und OpenAI-Schnittstelle"],
  ])("names the paid route of %j", (measured, text) => {
    expect(paidRouteText(measured)).toBe(text);
  });

  it.each([
    {},
    { route: "browser" },
    { route: "claude_code" },
    { routes: ["browser", "google_maps"] },
    { route: 42 },
    { routes: null },
    { route: "unbekannt" },
  ])("shows nothing for free, unknown or malformed routes in %j", (measured) => {
    expect(paidRouteText(measured)).toBeNull();
  });
});
