// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { costStatement } from "../lib/costs";
import { CostStatement } from "./CostStatement";

let root: HTMLDivElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.append(root);
});

afterEach(() => {
  render(null, root);
  root.remove();
});

async function show(rows: Parameters<typeof costStatement>[0]): Promise<string> {
  await act(() => {
    render(<CostStatement statement={costStatement(rows)} />, root);
  });
  return root.textContent;
}

const AUDIT = "a1";

describe("CostStatement", () => {
  it("shows calls, unit price, total and the grand total of the paid rows", async () => {
    const text = await show([{ audit_id: AUDIT, source: "apify", service: "instagram", calls: 4, cost: 0.02, tokens_in: 0, tokens_out: 0 }]);
    expect(text).toContain("Apify");
    expect(text).toContain("Instagram");
    expect(text).toContain("0,005");
    expect(text).toContain("Gesamtsumme");
  });

  it("says that nothing paid was booked", async () => {
    expect(await show([])).toContain("keine bezahlte Abfrage");
  });

  it("shows the Claude use without an amount", async () => {
    const text = await show([{ audit_id: AUDIT, source: "claude", service: "assistants", calls: 3, cost: 0, tokens_in: 12345, tokens_out: 6 }]);
    expect(text).toContain("Verbrauch ohne Eurobetrag");
    expect(text).toContain("12.345");
    expect(text).not.toContain("Gesamtsumme");
  });
});