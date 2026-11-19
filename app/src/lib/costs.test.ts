import { describe, expect, it } from "vitest";
import type { AuditCost } from "../api/types";
import { costStatement } from "./costs";

function row(source: string, service: string, calls: number, cost: number, tokensIn = 0, tokensOut = 0): AuditCost {
  return { audit_id: "a1", source, service, calls, cost, tokens_in: tokensIn, tokens_out: tokensOut };
}

describe("costStatement", () => {
  it("names a paid row, with the unit price as total per call", () => {
    const { paid } = costStatement([row("apify", "instagram", 4, 0.02)]);
    expect(paid).toEqual([{ source: "Apify", service: "Instagram", calls: 4, unitPrice: 0.005, total: 0.02 }]);
  });

  it("names the OpenAI interface row and prices it per call", () => {
    const { paid } = costStatement([row("chatgpt", "openai_chat_completions", 4, 0.002, 400, 800)]);
    expect(paid).toEqual([
      {
        source: "OpenAI-Schnittstelle (Antworten von ChatGPT)",
        service: "Fragen an ChatGPT, abgerechnet nach Verbrauch",
        calls: 4,
        unitPrice: 0.0005,
        total: 0.002,
      },
    ]);
  });

  it("adds up the paid rows only", () => {
    const { total } = costStatement([row("apify", "x", 1, 0.01), row("dataforseo", "/v3/backlinks/summary/live", 2, 0.002), row("claude", "assistants", 3, 0, 5, 6)]);
    expect(total).toBeCloseTo(0.012, 10);
  });

  it("lists Claude by use without a price", () => {
    const statement = costStatement([row("claude", "assistants", 3, 0, 100, 20)]);
    expect(statement.paid).toEqual([]);
    expect(statement.usage).toEqual([{ source: "Claude (im Abo enthalten)", service: "Fragen an den KI-Assistenten", calls: 3, tokensIn: 100, tokensOut: 20 }]);
  });

  it("keeps the key of an unknown source or service", () => {
    const { paid } = costStatement([row("newsource", "newservice", 1, 0.5)]);
    expect([paid[0]?.source, paid[0]?.service]).toEqual(["newsource", "newservice"]);
  });

  it("gives a row without calls no unit price", () => {
    expect(costStatement([row("apify", "x", 0, 0.01)]).paid[0]?.unitPrice).toBeNull();
  });

  it("gives no rows an empty statement", () => {
    expect(costStatement([])).toEqual({ paid: [], total: 0, usage: [] });
  });
});