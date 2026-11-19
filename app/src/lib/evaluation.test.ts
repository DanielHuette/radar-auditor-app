import { describe, expect, it } from "vitest";
import type { CheckOutcome, CheckResult } from "../api/types";
import {
  buildRows,
  checkAnchor,
  formatLeverPoints,
  topLevers,
  formatPotential,
  groupRows,
  orphanFindings,
  potentialTotals,
  previousAudit,
  radarAxes,
  sortRows,
} from "./evaluation";
import { catalogueCheck } from "./catalogue";
import { audit, check, finding } from "./testRows";

function scored(checkId: string, outcome: CheckOutcome, potential: number | null): CheckResult {
  return {
    ...check(catalogueCheck(checkId)?.area ?? "z", outcome, checkId),
    potential_points: potential,
  };
}

const ids = (rows: { check: CheckResult }[]) => rows.map((row) => row.check.check_id);

describe("buildRows", () => {
  it("joins findings and takes names from the catalogue", () => {
    const [row, unknown] = buildRows(
      [scored("a2", "failed", 2), scored("z9", "passed", 0)],
      [finding({ check_id: "a2", severity: 3 })],
    );
    expect(row).toMatchObject({ name: "Die Seite lädt am Handy schnell und ruhig", urgency: 3 });
    expect(unknown).toMatchObject({ name: "Prüfung z9", urgency: 0, finding: null });
  });
});

describe("sortRows", () => {
  const rows = buildRows(
    [scored("a4", "partial", 1), scored("b5", "failed", 4), scored("a2", "failed", 4), scored("c4", "passed", null)],
    [finding({ check_id: "a4", severity: 3 }), finding({ check_id: "b5", severity: 1 })],
  );

  it("puts the largest potential first, then the more urgent, then catalogue order", () => {
    expect(ids(sortRows(rows, "potential"))).toEqual(["b5", "a2", "a4", "c4"]);
  });

  it("orders by urgency, then potential", () => {
    expect(ids(sortRows(rows, "urgency"))).toEqual(["a4", "b5", "a2", "c4"]);
  });

  it("keeps the catalogue order for the area view", () => {
    expect(ids(sortRows(rows, "area"))).toEqual(["a2", "a4", "b5", "c4"]);
  });

  it("orders checks outside the catalogue with two-digit numbers naturally", () => {
    expect(ids(sortRows(buildRows([scored("x10", "passed", 0), scored("x9", "passed", 0)], []), "area"))).toEqual(["x9", "x10"]);
  });
});

describe("groupRows", () => {
  const rows = buildRows(
    [scored("a2", "failed", 1), scored("a4", "passed", 0), scored("a14", "failed", 5), scored("h1", "passed", 0)],
    [finding({ check_id: "a2", severity: 3 })],
  );

  it("orders areas by their open potential when sorting by potential", () => {
    const groups = groupRows(rows, "potential", false);
    expect(groups.map((group) => [group.area, group.potential])).toEqual([["bing", 5], ["google", 1], ["apple", 0]]);
    expect(groups[0]?.name).toBe("Bing und Copilot");
  });

  it("orders areas by their most urgent check", () => {
    expect(groupRows(rows, "urgency", false).map((group) => group.area)).toEqual(["google", "bing", "apple"]);
  });

  it("keeps catalogue order for areas", () => {
    expect(groupRows(rows, "area", false).map((group) => group.area)).toEqual(["google", "bing", "apple"]);
  });

  it("keeps only failed and partial checks with the open filter", () => {
    const groups = groupRows(rows, "area", true);
    expect(groups.map((group) => ids(group.rows))).toEqual([["a2"], ["a14"]]);
  });
});

describe("potentialTotals", () => {
  it("adds the potential of the open checks only", () => {
    const totals = potentialTotals([
      scored("a2", "failed", 2.5),
      scored("a4", "partial", 1),
      scored("a12", "passed", 0),
      scored("a11", "not_measurable", 3),
    ]);
    expect(totals).toEqual({ points: 3.5, openChecks: 2, known: true });
  });

  it("leaves out information checks and checks a control box marked wrong", () => {
    const wrong = { ...scored("a2", "failed", 2), verdict_evaluation_ok: false, verdict_evaluation_reason: "Schwelle falsch" };
    const totals = potentialTotals([wrong, scored("p3", "failed", 5), scored("a4", "failed", 1)]);
    expect(totals).toMatchObject({ points: 1, openChecks: 1 });
  });

  it("reports unknown potential for audits scored before potentials existed", () => {
    expect(potentialTotals([scored("a2", "failed", null)])).toMatchObject({ points: 0, known: false, openChecks: 1 });
  });
});

describe("formatPotential", () => {
  it("writes points with a sign and German decimals", () => {
    expect(formatPotential(2.5)).toBe("+2,5 Punkte");
    expect(formatPotential(1)).toBe("+1 Punkt");
  });
});

describe("previousAudit", () => {
  const current = audit({ id: "now", requested_at: "2026-10-01T08:00:00Z", total_score: 63 });

  it("picks the latest earlier audit with a score", () => {
    const history = [
      current,
      audit({ id: "failed", requested_at: "2026-09-20T08:00:00Z", status: "failed", total_score: null }),
      audit({ id: "august", requested_at: "2026-08-04T08:00:00Z", total_score: 41 }),
      audit({ id: "june", requested_at: "2026-06-01T08:00:00Z", total_score: 30 }),
      audit({ id: "later", requested_at: "2026-10-15T08:00:00Z", total_score: 70 }),
    ];
    expect(previousAudit(history, current)?.id).toBe("august");
  });

  it("returns null for the first audit of a customer", () => {
    expect(previousAudit([current], current)).toBeNull();
  });
});

describe("radarAxes", () => {
  it("gives one axis per weighted area with current and previous score", () => {
    const axes = radarAxes([scored("a2", "passed", 0), scored("a4", "failed", 2)], [scored("a2", "failed", 2)]);
    expect(axes.map((axis) => axis.area)).toEqual(["google", "bing", "apple", "openai", "anthropic"]);
    expect(axes[0]).toEqual({ area: "google", label: "Google", name: "Google", score: 50, previous: 0 });
    expect(axes[1]).toMatchObject({ area: "bing", score: null, previous: null });
    expect(axes.some((axis) => axis.area === "ads")).toBe(false);
  });

  it("has no previous scores without a previous audit", () => {
    expect(radarAxes([scored("a2", "passed", 0)], null).every((axis) => axis.previous === null)).toBe(true);
  });
});

describe("orphanFindings", () => {
  it("returns findings whose check has no result", () => {
    const orphan = finding({ check_id: "x1" });
    expect(orphanFindings([scored("a2", "failed", 1)], [finding({ check_id: "a2" }), orphan])).toEqual([orphan]);
  });
});

describe("topLevers", () => {
  it("picks the three open checks with the most potential, largest first", () => {
    const checks = [
      scored("a2", "failed", 1.5),
      scored("a14", "partial", 4),
      { ...scored("j1", "failed", 2.25), measured: { answers: 4 } },
      scored("b10", "failed", 0.5),
      scored("b5", "passed", 9),
    ];
    const levers = topLevers(checks);
    expect(levers.map((lever) => [lever.checkId, lever.potential])).toEqual([["a14", 4], ["j1", 2.25], ["a2", 1.5]]);
    expect(levers[1]).toEqual({ checkId: "j1", name: expect.any(String) as string, measured: "Antworten: 4", potential: 2.25 });
    expect(levers[0]?.measured).toBeNull();
  });

  it("leaves out checks without open potential and keeps catalogue order on ties", () => {
    const levers = topLevers([scored("b10", "failed", 2), scored("a4", "failed", 2), scored("a12", "failed", 0), scored("a11", "partial", null)]);
    expect(levers.map((lever) => lever.checkId)).toEqual(["a4", "b10"]);
    expect(topLevers([])).toEqual([]);
  });

  it("uses the finding's sentence as the measured line when there is one", () => {
    const check = scored("j1", "failed", 2);
    const finding = { id: "f1", audit_id: "a1", check_id: "j1", severity: 2 as const, title: "t", explanation: "Von zehn neuen Bewertungen haben Sie vier beantwortet.", customer_impact: "c", fix: "x", fix_payload: null };
    expect(topLevers([check], [finding])[0]?.measured).toBe("Von zehn neuen Bewertungen haben Sie vier beantwortet.");
  });

  it("formats the pill and names the row anchor", () => {
    expect(formatLeverPoints(2.25)).toBe("+2,3");
    expect(formatLeverPoints(4)).toBe("+4");
    expect(checkAnchor("j1")).toBe("pruefung-j1");
  });
});
