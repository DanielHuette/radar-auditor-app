import { PDFDocument } from "pdf-lib";
import { describe, expect, it } from "vitest";
import { estimateStatement } from "./costs";
import { estimatePdf } from "./estimatePdf";

const ROWS = [
  { audit_id: "a1", source: "apify", service: "apple_maps", calls: 1, unit_cost: 0.02, cost: 0.02 },
  { audit_id: "a1", source: "dataforseo", service: "/v3/serp/google/organic/live/advanced", calls: 39, unit_cost: 0.0035, cost: 0.1365 },
  { audit_id: "a1", source: "claude", service: "assistants", calls: 20, unit_cost: 0, cost: 0 },
];

function document(customerName: string, rows = ROWS) {
  return { customerName, requestedAt: "2026-11-13T07:38:32Z", verdict: "Guthaben und Limits decken den Voranschlag", statement: estimateStatement(rows) };
}

describe("estimatePdf", () => {
  it("writes a one-page PDF titled after the customer", async () => {
    const pdf = await PDFDocument.load(await estimatePdf(document("Katja Karsch")));
    expect(pdf.getPageCount()).toBe(1);
    expect(pdf.getTitle()).toBe("Kostenvoranschlag Katja Karsch");
  });

  it("accepts a customer name with letters the print font does not have", async () => {
    const bytes = await estimatePdf(document("Łukasz Bäckerei 🍞"));
    expect(new TextDecoder().decode(bytes.slice(0, 5))).toBe("%PDF-");
  });

  it("continues on a second page when the lines do not fit on one", async () => {
    const many = Array.from({ length: 60 }, (_, index) => ({ ...ROWS[0], service: `service-${String(index)}` })) as typeof ROWS;
    const pdf = await PDFDocument.load(await estimatePdf(document("Katja Karsch", many)));
    expect(pdf.getPageCount()).toBe(2);
  });
});
