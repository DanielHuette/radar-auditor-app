import { describe, expect, it } from "vitest";
import type { AuditEvent, CheckOutcome, CheckResult, EventResult } from "../api/types";
import { CATALOGUE_AREAS, catalogueCheck } from "./catalogue";
import { barPercent, BAR_LABELS, BAR_STAGES, checkProgress, controlWaitText, invalidStepReasons, stepsOfCheck, stepStates, type StepState } from "./checkProgress";
import { check } from "./testRows";

function event(step: string, result: EventResult, stage?: string, reason: string | null = null): AuditEvent {
  return {
    occurred_at: "2026-11-15T08:00:00Z",
    kind: "measurement",
    result,
    label: step,
    reason,
    stage: null,
    route: `messung:${step}`,
    check_id: null,
    protocol: stage === undefined ? null : { stage },
  };
}

function states(...pairs: [string, StepState][]): Map<string, StepState> {
  return new Map(pairs);
}

function result(outcome: CheckOutcome, overrides: Partial<CheckResult> = {}): CheckResult {
  return { ...check("google", outcome, "a2"), ...overrides };
}

describe("stepsOfCheck", () => {
  it.each([
    ["a2", ["pagespeed"]],
    ["a4", ["website"]],
    ["c4", ["website", "company"]],
    ["f1", ["place"]],
    ["j9", ["search_google"]],
    ["c12", ["links"]],
    ["j1", ["search_google"]],
    ["j7", ["search_bing"]],
    ["j8", ["search_images"]],
    ["i1", ["links"]],
    ["k1", ["assistants"]],
    ["h1", ["listings"]],
    ["p1", ["ads_google"]],
    ["p2", ["ads_meta"]],
    ["m1", []],
  ])("maps %s to its measuring steps", (id, steps) => {
    expect(stepsOfCheck(catalogueCheck(id))).toEqual(steps);
  });

  it("gives every check of the catalogue only steps the collector knows", () => {
    const known = new Set(["website", "company", "pagespeed", "search_google", "search_bing", "search_images", "place", "links", "assistants", "listings", "ads_google", "ads_meta"]);
    for (const area of CATALOGUE_AREAS) {
      for (const entry of area.checks) {
        for (const step of stepsOfCheck(entry)) expect(known.has(step), `${entry.id}: ${step}`).toBe(true);
      }
    }
  });

  it("has no steps for a check outside the catalogue", () => {
    expect(stepsOfCheck(undefined)).toEqual([]);
  });
});

describe("stepStates", () => {
  it("follows holen, pruefen and the final verdict of a step", () => {
    expect(stepStates([event("place", "läuft", "holen")]).get("place")).toBe("fetching");
    expect(stepStates([event("place", "läuft", "holen"), event("place", "läuft", "pruefen")]).get("place")).toBe("validating");
    expect(stepStates([event("place", "läuft", "pruefen"), event("place", "gültig")]).get("place")).toBe("valid");
    expect(stepStates([event("place", "läuft", "pruefen"), event("place", "ungültig", undefined, "leer")]).get("place")).toBe("invalid");
  });

  it("takes a running event without stage as fetching", () => {
    expect(stepStates([event("place", "läuft")]).get("place")).toBe("fetching");
  });

  it("ignores events that are no measurement steps", () => {
    const other: AuditEvent = { ...event("place", "gültig"), kind: "route" };
    const control: AuditEvent = { ...event("x", "gemessen"), route: "kontrolle:a2" };
    expect(stepStates([other, control]).size).toBe(0);
  });
});

describe("invalidStepReasons", () => {
  it("keeps the reason of an invalid step until a valid result replaces it", () => {
    expect(invalidStepReasons([event("place", "ungültig", undefined, "Ort falsch")]).get("place")).toBe("Ort falsch");
    expect(invalidStepReasons([event("place", "ungültig", undefined, "Ort falsch"), event("place", "läuft", "holen")]).get("place")).toBe("Ort falsch");
    expect(invalidStepReasons([event("place", "ungültig", undefined, "Ort falsch"), event("place", "gültig")]).has("place")).toBe(false);
    expect(invalidStepReasons([event("place", "ungültig", undefined, "  ")]).has("place")).toBe(false);
  });
});

describe("checkProgress without a result", () => {
  const steps = ["website", "company"];
  const input = (map: Map<string, StepState>, auditStatus: "collecting" | "collected" = "collecting") => ({ auditStatus, steps, states: map, result: null });

  it("waits until a step reports", () => {
    expect(checkProgress(input(states())).stage).toBe("waiting");
  });

  it("fetches while a step runs with holen", () => {
    expect(checkProgress(input(states(["website", "fetching"]))).stage).toBe("fetching");
  });

  it("validates while a step runs with pruefen, even if another still fetches", () => {
    expect(checkProgress(input(states(["website", "validating"], ["company", "fetching"]))).stage).toBe("validating");
  });

  it("keeps fetching while one of two steps has a verdict and the other has none", () => {
    expect(checkProgress(input(states(["website", "valid"], ["company", "fetching"]))).stage).toBe("fetching");
    expect(checkProgress(input(states(["website", "valid"]))).stage).toBe("waiting");
  });

  it("evaluates when every step has a verdict, valid or invalid", () => {
    expect(checkProgress(input(states(["website", "valid"], ["company", "invalid"]))).stage).toBe("evaluating");
  });

  it("has no steps for the customer-access checks and evaluates only once the audit has collected", () => {
    const access = { auditStatus: "collecting" as const, steps: [], states: states(), result: null };
    expect(checkProgress(access).stage).toBe("waiting");
    expect(checkProgress({ ...access, auditStatus: "collected" }).stage).toBe("evaluating");
    expect(checkProgress({ ...access, auditStatus: "scored" }).stage).toBe("evaluating");
  });
});

describe("checkProgress with a result", () => {
  const base = { auditStatus: "controlling" as const, steps: [], states: states() };

  it("is in control while the verdict is open", () => {
    expect(checkProgress({ ...base, result: result("passed") })).toEqual({ stage: "control", wait: null });
  });

  it("is in control for a not measurable check too", () => {
    expect(checkProgress({ ...base, result: result("not_measurable") }).stage).toBe("control");
  });

  it("names the failed attempt and its reason", () => {
    const waiting = result("failed", { control_attempt: 2, control_last_error: "Zeitüberschreitung" });
    expect(checkProgress({ ...base, result: waiting }).wait).toEqual({ attempt: 2, error: "Zeitüberschreitung" });
  });

  it("is done once the control is done", () => {
    expect(checkProgress({ ...base, result: result("passed", { control_state: "done" }) })).toEqual({ stage: "done", wait: null });
  });

  it.each<CheckOutcome>(["skipped", "not_applicable"])("is done for a %s check without any control", (outcome) => {
    expect(checkProgress({ ...base, result: result(outcome) }).stage).toBe("done");
  });
});

describe("bar", () => {
  it("lists the six stages in the agreed order with their labels", () => {
    expect(BAR_STAGES.map((stage) => BAR_LABELS[stage])).toEqual(["wartet", "Daten werden geholt", "Daten werden geprüft", "wird ausgewertet", "Kontrolle", "fertig"]);
  });

  it("fills from empty to full", () => {
    expect(barPercent("waiting")).toBe(0);
    expect(barPercent("control")).toBe(80);
    expect(barPercent("done")).toBe(100);
  });

  it("words the waiting control with attempt and reason", () => {
    expect(controlWaitText({ attempt: 3, error: "Agent nicht erreichbar" })).toBe("Kontrolle wartet – Versuch 3: Agent nicht erreichbar");
    expect(controlWaitText({ attempt: 1, error: null })).toBe("Kontrolle wartet – Versuch 1");
  });
});
