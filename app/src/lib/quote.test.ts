import { describe, expect, it } from "vitest";
import type { CrmProduct } from "../api/types";
import {
  buildQuoteItems,
  computeTotals,
  openChecksByArea,
  parseQuantity,
  proposePositions,
  quoteTexts,
  type CheckOutcomeRow,
  type Position,
} from "./quote";

function product(overrides: Partial<CrmProduct>): CrmProduct {
  return {
    id: "p",
    name: "Produkt",
    description: "",
    unit: "Pauschale",
    price_net: 100,
    vat_rate: 19,
    billing: "once",
    active: true,
    radar_areas: [],
    ...overrides,
  };
}

function position(overrides: Partial<Position>): Position {
  return {
    productId: "p",
    title: "Produkt",
    description: "",
    unit: "Pauschale",
    unitPriceNet: 100,
    vatRate: 19,
    billing: "once",
    areas: ["google"],
    openChecks: 1,
    quantity: 1,
    enabled: true,
    ...overrides,
  };
}

const CHECKS: CheckOutcomeRow[] = [
  { check_id: "a2", area: "google", outcome: "failed" },
  { check_id: "a4", area: "google", outcome: "partial" },
  { check_id: "a12", area: "google", outcome: "passed" },
  { check_id: "a14", area: "bing", outcome: "failed" },
  { check_id: "h1", area: "apple", outcome: "passed" },
  { check_id: "a15", area: "openai", outcome: "not_measurable" },
  { check_id: "a17", area: "anthropic", outcome: "skipped" },
];

describe("openChecksByArea", () => {
  it("counts only failed and partial checks", () => {
    expect([...openChecksByArea(CHECKS)]).toEqual([
      ["google", 2],
      ["bing", 1],
    ]);
  });
});

describe("proposePositions", () => {
  it("proposes each product once whose areas have open checks, one-off before monthly", () => {
    const positions = proposePositions(CHECKS, [
      product({ id: "care", name: "Laufende Betreuung", billing: "monthly", price_net: 290, radar_areas: ["bing", "apple"] }),
      product({ id: "setup", name: "Erstoptimierung", radar_areas: ["bing", "google", "zz"] }),
      product({ id: "setup", name: "Erstoptimierung", radar_areas: ["google"] }),
      product({ id: "links", name: "Linkaufbau", radar_areas: ["openai"] }),
      product({ id: "old", name: "Altprodukt", active: false, radar_areas: ["google"] }),
    ]);
    expect(positions.map((item) => item.productId)).toEqual(["setup", "care"]);
    expect(positions[0]).toMatchObject({ areas: ["google", "bing"], openChecks: 3, quantity: 1, enabled: true, billing: "once" });
    expect(positions[1]).toMatchObject({ areas: ["bing"], openChecks: 1, billing: "monthly", unitPriceNet: 290 });
  });

  it("proposes nothing when the CRM has no products", () => {
    expect(proposePositions(CHECKS, [])).toEqual([]);
  });

  it("proposes nothing when every check passed", () => {
    const passed = CHECKS.map((check) => ({ ...check, outcome: "passed" as const }));
    expect(proposePositions(passed, [product({ radar_areas: ["google"] })])).toEqual([]);
  });
});

describe("parseQuantity", () => {
  it("accepts whole numbers from 1 to 999", () => {
    expect(parseQuantity(" 3 ")).toBe(3);
    expect(parseQuantity("999")).toBe(999);
  });

  it("rejects zero, fractions, text and empty input", () => {
    for (const input of ["0", "1,5", "1000", "zwei", "", "-1"]) expect(parseQuantity(input)).toBeNull();
  });
});

describe("computeTotals", () => {
  it("sums enabled positions with quantity and VAT per rate", () => {
    const totals = computeTotals(
      [
        position({ unitPriceNet: 490, quantity: 2 }),
        position({ unitPriceNet: 290, billing: "monthly" }),
        position({ unitPriceNet: 50, vatRate: 7 }),
        position({ unitPriceNet: 1000, enabled: false }),
      ],
      false,
    );
    expect(totals).toEqual({ net: 1320, vat: 244.8, gross: 1564.8, onceNet: 1030, monthlyNet: 290 });
  });

  it("rounds VAT on the sum per rate like the CRM", () => {
    // 19 % of the sum 99.99 is 19.00; rounding each line (6.33 three times) would give 18.99.
    const totals = computeTotals([1, 2, 3].map((n) => position({ productId: String(n), unitPriceNet: 33.33 })), false);
    expect(totals.vat).toBe(19);
    expect(totals.gross).toBe(118.99);
  });

  it("charges no VAT for a small business", () => {
    expect(computeTotals([position({ unitPriceNet: 490 })], true)).toMatchObject({ net: 490, vat: 0, gross: 490 });
  });

  it("is zero without enabled positions", () => {
    expect(computeTotals([position({ enabled: false })], false)).toEqual({ net: 0, vat: 0, gross: 0, onceNet: 0, monthlyNet: 0 });
  });
});

describe("buildQuoteItems", () => {
  it("numbers the enabled positions and names the billing", () => {
    const items = buildQuoteItems("q1", [
      position({ productId: "setup", title: "Erstoptimierung", description: "Einmalige Umsetzung", areas: ["google", "bing"] }),
      position({ productId: "skip", enabled: false }),
      position({ productId: "care", title: "Laufende Betreuung", billing: "monthly", quantity: 2, unitPriceNet: 290 }),
    ]);
    expect(items).toEqual([
      {
        quote_id: "q1",
        position: 1,
        product_id: "setup",
        title: "Erstoptimierung, einmalig",
        description: "Einmalige Umsetzung\nBehebt die offenen Punkte in: Google, Bing und Copilot.",
        quantity: 1,
        unit: "Pauschale",
        unit_price_net: 100,
        vat_rate: 19,
      },
      {
        quote_id: "q1",
        position: 2,
        product_id: "care",
        title: "Laufende Betreuung, monatlich",
        description: "Behebt die offenen Punkte in: Google.",
        quantity: 2,
        unit: "Pauschale",
        unit_price_net: 290,
        vat_rate: 19,
      },
    ]);
  });

  it("keeps titles within the CRM limit of 300 characters", () => {
    const [item] = buildQuoteItems("q1", [position({ title: "x".repeat(400), billing: "monthly" })]);
    expect(item?.title).toHaveLength(300);
    expect(item?.title.endsWith(", monatlich")).toBe(true);
  });
});

describe("quoteTexts", () => {
  it("refers to score, audit date and covered areas", () => {
    const { intro, outro } = quoteTexts({ score: 62.6, auditDate: "2026-10-01T08:30:00+02:00", areas: ["bing", "google", "apple", "google"] });
    expect(intro).toBe(
      "vielen Dank für das Gespräch. Die Prüfung Ihres Online-Auftritts vom 01.10.2026 hat 63 von 100 Punkten ergeben. " +
        "Die folgenden Leistungen beheben die offenen Punkte in den Bereichen Google, " +
        "Bing und Copilot und Apple:",
    );
    expect(outro).toContain("Vorher-Nachher-Vergleich");
  });

  it("works without score, date and areas", () => {
    const { intro } = quoteTexts({ score: null, auditDate: null, areas: [] });
    expect(intro).toBe(
      "vielen Dank für das Gespräch. Die Prüfung Ihres Online-Auftritts zeigt, wo Kunden Sie heute noch nicht finden. " +
        "Gern biete ich Ihnen folgende Leistungen an:",
    );
  });
});


describe("openChecksByArea with the control", () => {
  it("does not count checks the control has crossed out", () => {
    const rows: CheckOutcomeRow[] = [
      { check_id: "a2", area: "google", outcome: "failed", verdict_collected_ok: true, verdict_data_ok: true, verdict_evaluation_ok: true },
      { check_id: "a4", area: "google", outcome: "failed", verdict_collected_ok: true, verdict_data_ok: false, verdict_evaluation_ok: null },
      { check_id: "a7", area: "google", outcome: "failed", verdict_collected_ok: false, verdict_data_ok: null, verdict_evaluation_ok: null },
    ];

    expect([...openChecksByArea(rows)]).toEqual([["google", 1]]);
  });

  it("does not count findings without rating", () => {
    expect([...openChecksByArea([{ check_id: "p1", area: "ads", outcome: "failed" }])]).toEqual([]);
  });
});
