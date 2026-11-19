import { describe, expect, it } from "vitest";
import { formatDate, formatEuro, formatEuroPrecise, formatNumber, hostOf, normaliseWebsite, parseAmount } from "./format";

describe("parseAmount", () => {
  it("reads German and plain notation", () => {
    expect(parseAmount("1.234,56")).toBe(1234.56);
    expect(parseAmount("490,5")).toBe(490.5);
    expect(parseAmount("490.50")).toBe(490.5);
    expect(parseAmount("149 €")).toBe(149);
  });

  it("rejects text, empty input and more than two decimals", () => {
    expect(parseAmount("")).toBeNull();
    expect(parseAmount("viel")).toBeNull();
    expect(parseAmount("1,234")).toBeNull();
  });
});

describe("normaliseWebsite", () => {
  it("adds https to a bare domain", () => {
    expect(normaliseWebsite("dachdecker-muster.de")).toBe("https://dachdecker-muster.de/");
  });

  it("keeps an explicit scheme", () => {
    expect(normaliseWebsite("http://dachdecker-muster.de/kontakt")).toBe("http://dachdecker-muster.de/kontakt");
  });

  it("rejects empty input and names without a domain", () => {
    expect(normaliseWebsite("  ")).toBeNull();
    expect(normaliseWebsite("dachdecker")).toBeNull();
  });
});

describe("display formats", () => {
  it("formats euros and dates in German", () => {
    expect(formatEuro(1234.5).replace(/\s/g, " ")).toBe("1.234,50 €");
    expect(formatEuro(null)).toBe("–");
    expect(formatDate("2026-10-16")).toBe("16.10.2026");
    expect(formatDate(null)).toBe("–");
  });

  it("extracts the host of a website", () => {
    expect(hostOf("https://dachdecker-muster.de/")).toBe("dachdecker-muster.de");
    expect(hostOf(null)).toBe("");
  });
});

describe("formatEuroPrecise", () => {
  it("shows up to four decimals so that a cent fraction is not rounded away", () => {
    expect(formatEuroPrecise(0.0045)).toContain("0,0045");
    expect(formatEuroPrecise(0.02)).toContain("0,02");
    expect(formatEuroPrecise(0.02)).not.toContain("0,0200");
  });

  it("shows a dash without an amount", () => {
    expect(formatEuroPrecise(null)).toBe("–");
  });
});

describe("formatNumber", () => {
  it("groups thousands the German way", () => {
    expect(formatNumber(12345)).toBe("12.345");
  });
});