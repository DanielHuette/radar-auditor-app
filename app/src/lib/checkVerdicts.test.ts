import { describe, expect, it } from "vitest";
import type { CheckOutcome, CheckResult } from "../api/types";
import { awaitsControl, BOX_LABELS, controlDone, controlRunning, countsInScore, isExcludedByControl, REASON_MISSING, verdictBoxes } from "./checkVerdicts";
import { check } from "./testRows";

function result(outcome: CheckOutcome, overrides: Partial<CheckResult> = {}, checkId = "a2"): CheckResult {
  return { ...check("google", outcome, checkId), ...overrides };
}

const states = (boxes: ReturnType<typeof verdictBoxes>) => boxes.map((box) => box.state);

describe("verdictBoxes", () => {
  it("names the three boxes in order", () => {
    expect(verdictBoxes({}).map((box) => box.label)).toEqual([BOX_LABELS.collected, BOX_LABELS.data, BOX_LABELS.evaluation]);
  });

  it("leaves all three empty while no verdict exists", () => {
    expect(states(verdictBoxes({}))).toEqual(["open", "open", "open"]);
    expect(states(verdictBoxes({ verdict_collected_ok: null, verdict_data_ok: null, verdict_evaluation_ok: null }))).toEqual(["open", "open", "open"]);
  });

  it("shows a check with the evidence for an ok verdict", () => {
    const boxes = verdictBoxes({ verdict_collected_ok: true, verdict_collected_evidence: "lcp_ms: 3120", verdict_data_ok: true, verdict_evaluation_ok: true });
    expect(states(boxes)).toEqual(["ok", "ok", "ok"]);
    expect(boxes[0]?.evidence).toBe("lcp_ms: 3120");
    expect(boxes[1]?.evidence).toBeNull();
  });

  it("shows a cross with the plain reason and the evidence for a failed verdict", () => {
    const boxes = verdictBoxes({
      verdict_collected_ok: true,
      verdict_data_ok: false,
      verdict_data_reason: "Die Zahl steht so nicht in den Rohdaten.",
      verdict_data_evidence: "cls: 0,4",
      verdict_evaluation_ok: true,
    });
    expect(states(boxes)).toEqual(["ok", "bad", "ok"]);
    expect(boxes[1]).toMatchObject({ reason: "Die Zahl steht so nicht in den Rohdaten.", evidence: "cls: 0,4" });
  });

  it("keeps a stored reason empty-safe for a cross without one", () => {
    const [first] = verdictBoxes({ verdict_collected_ok: false, verdict_collected_reason: "  " });
    expect(first).toMatchObject({ state: "bad", reason: REASON_MISSING });
  });

  it("leaves boxes 2 and 3 empty when box 1 is a cross, whatever is stored for them", () => {
    const boxes = verdictBoxes({
      verdict_collected_ok: false,
      verdict_collected_reason: "Daten unvollständig",
      verdict_data_ok: true,
      verdict_data_evidence: "x: 1",
      verdict_evaluation_ok: false,
      verdict_evaluation_reason: "falsch",
    });
    expect(states(boxes)).toEqual(["bad", "open", "open"]);
    expect(boxes[1]).toMatchObject({ reason: null, evidence: null });
    expect(boxes[2]).toMatchObject({ reason: null, evidence: null });
  });

  it("does not hide boxes 2 and 3 when box 1 is ok", () => {
    expect(states(verdictBoxes({ verdict_collected_ok: true, verdict_data_ok: false, verdict_data_reason: "a", verdict_evaluation_ok: false, verdict_evaluation_reason: "b" }))).toEqual(["ok", "bad", "bad"]);
  });
});

describe("exclusion from the total value", () => {
  it.each([
    [{ verdict_collected_ok: false }, true],
    [{ verdict_data_ok: false }, true],
    [{ verdict_evaluation_ok: false }, true],
    [{ verdict_collected_ok: true, verdict_data_ok: true, verdict_evaluation_ok: true }, false],
    [{}, false],
  ])("marks %j as excluded: %s", (verdicts, excluded) => {
    expect(isExcludedByControl(verdicts)).toBe(excluded);
  });

  it("counts judged checks without a cross and leaves out crosses and information checks", () => {
    expect(countsInScore(result("passed"))).toBe(true);
    expect(countsInScore(result("failed", { verdict_data_ok: false }))).toBe(false);
    expect(countsInScore(result("not_measurable"))).toBe(false);
    expect(countsInScore(result("passed", {}, "p1"))).toBe(false);
  });
});

describe("control state", () => {
  it("reads done only from control_state done", () => {
    expect(controlDone({ control_state: "done" })).toBe(true);
    expect(controlDone({ control_state: "open" })).toBe(false);
    expect(controlDone({})).toBe(false);
  });

  it.each<[CheckOutcome, boolean]>([
    ["passed", true],
    ["partial", true],
    ["failed", true],
    ["not_measurable", true],
    ["skipped", false],
    ["not_applicable", false],
  ])("awaits a verdict for %s: %s", (outcome, awaits) => {
    expect(awaitsControl(result(outcome))).toBe(awaits);
  });

  it("stops awaiting once done", () => {
    expect(awaitsControl(result("passed", { control_state: "done" }))).toBe(false);
  });

  it("runs while any check still awaits a verdict", () => {
    expect(controlRunning([result("passed", { control_state: "done" }), result("failed")])).toBe(true);
    expect(controlRunning([result("passed", { control_state: "done" }), result("skipped")])).toBe(false);
    expect(controlRunning([])).toBe(false);
  });
});
