import { describe, expect, it } from "vitest";
import type { CheckResult } from "../api/types";
import { checkLine } from "./checkLine";
import { awaitsControl, controlRunning, isFailedMeasurement, isPlannedNotMeasurable } from "./checkVerdicts";
import { catalogueCheck, check, EMPTY_CONTEXT } from "./testRows";

function notMeasurable(checkId: string, measured: Record<string, unknown>, overrides: Partial<CheckResult> = {}): CheckResult {
  return { ...check("google", "not_measurable", checkId), measured, ...overrides };
}

describe("planned not measurable checks", () => {
  it.each(["m1", "m2", "p5", "p6", "b12", "j9"])("treats %s as planned without a reason code", (id) => {
    expect(isPlannedNotMeasurable(notMeasurable(id, {}))).toBe(true);
  });

  it.each(["no_access", "paid_disabled", "needs_history", "ownership_not_verifiable", "not_verifiable_from_outside"])(
    "treats the reason %s as planned",
    (reason) => {
      expect(isPlannedNotMeasurable(notMeasurable("a2", { reason }))).toBe(true);
    },
  );

  it("never waits for the control and shows a finished bar", () => {
    const result = notMeasurable("m1", { reason: "no_access" });

    const line = checkLine(catalogueCheck("m1"), result, EMPTY_CONTEXT);

    expect(awaitsControl(result)).toBe(false);
    expect(line.progress).toEqual({ stage: "done", wait: null });
    expect(line.boxes).toEqual([]);
    expect(line.statusLabel).toBe("nicht gemessen");
    expect(line.excluded).toBe(false);
  });

  it("does not keep the control of the audit running", () => {
    expect(controlRunning([notMeasurable("m1", { reason: "no_access" }), notMeasurable("p5", {})])).toBe(false);
  });
});

describe("failed measurements", () => {
  const failed = notMeasurable("a2", { reason: "measurement_invalid", invalid_reason: "Werte außerhalb des Bereichs" });

  it("are not planned and wait for the first verdict of the control", () => {
    const line = checkLine(catalogueCheck("a2"), failed, EMPTY_CONTEXT);

    expect(isFailedMeasurement(failed)).toBe(true);
    expect(awaitsControl(failed)).toBe(true);
    expect(line.progress?.stage).toBe("control");
  });

  it("show not measured with the reason and only the first verdict", () => {
    const done = { ...failed, control_state: "done" as const, verdict_collected_ok: true, verdict_data_ok: null, verdict_evaluation_ok: null };

    const line = checkLine(catalogueCheck("a2"), done, EMPTY_CONTEXT);

    expect(line.statusLabel).toBe("nicht gemessen");
    expect(line.reason).toBe("Werte außerhalb des Bereichs");
    expect(line.progress).toEqual({ stage: "done", wait: null });
    expect(line.boxes.map((box) => box.state)).toEqual(["ok", "open", "open"]);
  });
});
