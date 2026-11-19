import { describe, expect, it } from "vitest";
import type { CheckOutcome, CheckResult } from "../api/types";
import { CATALOGUE_AREAS } from "./catalogue";
import { buildRows, notMeasuredReason, REASON_DESELECTED, REASON_NO_RESULT, REASON_NOT_STORED, unmeasuredChecks } from "./evaluation";
import { check } from "./testRows";

function result(checkId: string, outcome: CheckOutcome, measured: Record<string, unknown>): CheckResult {
  return { ...check(checkId.replace(/\d+$/, ""), outcome, checkId), measured };
}

describe("notMeasuredReason", () => {
  it.each<CheckOutcome>(["passed", "partial", "failed"])("gives no reason for a measured result (%s)", (outcome) => {
    expect(notMeasuredReason(result("a1", outcome, { reason: "no_access" }), "ignored")).toBeNull();
  });

  it.each<CheckOutcome>(["not_measurable", "skipped", "not_applicable"])("takes the stored reason of a %s check", (outcome) => {
    expect(notMeasuredReason(result("a1", outcome, { reason: "Tageslimit erreicht" }), "event")).toBe("Tageslimit erreicht");
  });

  it("falls back to the reason of the check event, then to naming that none was stored", () => {
    const unmeasured = result("a1", "not_measurable", {});
    expect(notMeasuredReason(unmeasured, " Bing blockt ")).toBe("Bing blockt");
    expect(notMeasuredReason(unmeasured, undefined)).toBe(REASON_NOT_STORED);
    expect(notMeasuredReason(unmeasured, "  ")).toBe(REASON_NOT_STORED);
    expect(notMeasuredReason(result("a1", "not_measurable", { reason: "  " }), undefined)).toBe(REASON_NOT_STORED);
  });

  it("is carried into the rows by check id", () => {
    const [row] = buildRows([result("a1", "not_measurable", {})], [], new Map([["a1", "Seite nicht erreichbar"]]));
    expect(row?.reason).toBe("Seite nicht erreichbar");
  });
});

describe("unmeasuredChecks", () => {
  const total = CATALOGUE_AREAS.flatMap((area) => area.checks).length;

  it("lists every catalogue check that has no result", () => {
    expect(unmeasuredChecks([], [])).toHaveLength(total);
    expect(unmeasuredChecks([result("a2", "passed", {})], [])).toHaveLength(total - 1);
  });

  it("says that a check of the customer accounts needs access to them", () => {
    const access = unmeasuredChecks([], []).find((entry) => entry.checkId === "m1");
    expect(access?.reason).toBe("Nur mit Zugang zu Ihren Konten bei den Anbietern (zum Beispiel Google, Meta) messbar.");
  });

  it("words a check that waits for a second audit as such", () => {
    expect(notMeasuredReason(result("j9", "not_measurable", { reason: "needs_history" }), undefined)).toBe("Erst ab der zweiten Prüfung messbar.");
  });

  it("says deselected for checks switched off and no result for the others", () => {
    const [first, second] = unmeasuredChecks([], ["a4"]).filter((entry) => entry.checkId === "a2" || entry.checkId === "a4");
    expect(first).toMatchObject({ checkId: "a2", reason: REASON_NO_RESULT });
    expect(second).toMatchObject({ checkId: "a4", reason: REASON_DESELECTED });
  });
});
