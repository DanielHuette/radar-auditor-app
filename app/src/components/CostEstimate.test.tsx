// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { AuditEstimate, EstimateStatus } from "../api/types";
import { CostEstimate } from "./CostEstimate";

let root: HTMLDivElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.append(root);
});

afterEach(() => {
  render(null, root);
  root.remove();
});

async function show(estimates: AuditEstimate[], status: EstimateStatus | null, note: string | null): Promise<string> {
  await act(() => {
    render(<CostEstimate estimates={estimates} status={status} note={note} customerName="Katja Karsch" requestedAt="2026-11-13T07:38:32Z" />, root);
  });
  return root.textContent;
}

const APIFY: AuditEstimate = { audit_id: "a1", source: "apify", service: "instagram", calls: 4, unit_cost: 0.005, cost: 0.02 };
const CLAUDE: AuditEstimate = { audit_id: "a1", source: "claude", service: "assistants", calls: 12, unit_cost: 0, cost: 0 };

describe("CostEstimate", () => {
  it("shows nothing before the collector priced the audit", async () => {
    expect(await show([], null, null)).toBe("");
  });

  it("shows calls, unit price, line total and the estimate total with the covered verdict", async () => {
    const text = await show([APIFY], "covered", "Voranschlag 0,02 € gedeckt");
    expect(text).toContain("Kostenvoranschlag");
    expect(text).toContain("Guthaben und Limits decken den Voranschlag: Voranschlag 0,02 € gedeckt");
    expect(text).toContain("4 Abrufe");
    expect(text).toContain("0,005");
    expect(text).toContain("Voranschlag gesamt");
  });

  it("names an insufficient estimate as an alert with its reason", async () => {
    const text = await show([APIFY], "insufficient", "DataForSEO: Guthaben fehlt");
    expect(root.querySelector('[role="alert"]')?.textContent).toBe(
      "Guthaben oder Limits reichen nicht, die Prüfung startet nicht: DataForSEO: Guthaben fehlt",
    );
    expect(text).toContain("Voranschlag gesamt");
  });

  it("offers the estimate as a PDF once the collector priced the audit", async () => {
    await show([APIFY], "covered", null);
    expect(root.querySelector("button")?.textContent).toBe("Kostenvoranschlag herunterladen (PDF)");
    await show([], "unchecked", null);
    expect(root.querySelector("button")).toBeNull();
  });

  it("shows the planned Claude calls without an amount", async () => {
    const text = await show([CLAUDE], "unchecked", null);
    expect(text).toContain("12 Aufrufe, ohne Eurobetrag");
    expect(text).not.toContain("Voranschlag gesamt");
    expect(text).toContain("Guthaben konnte nicht geprüft werden");
  });
});
