import { describe, expect, it } from "vitest";
import { areaScore, groupByArea, scoreHistory, scoreTone, sortFindings } from "./scoring";
import type { CheckOutcome } from "../api/types";
import { audit, check, finding } from "./testRows";

describe("areaScore", () => {
  it("counts passed as 1, partial as 0.5 and failed as 0", () => {
    expect(areaScore([check("a", "passed"), check("a", "partial"), check("a", "failed"), check("a", "failed")])).toBe(37.5);
  });

  it("leaves unmeasurable checks out of the share", () => {
    expect(areaScore([check("a", "passed"), check("a", "not_measurable")])).toBe(100);
  });

  it("leaves deselected checks out of the share", () => {
    expect(areaScore([check("a", "passed"), check("a", "skipped", "a2")])).toBe(100);
    expect(areaScore([check("a", "skipped")])).toBeNull();
  });

  it("returns null when nothing in the area was measurable", () => {
    expect(areaScore([check("a", "not_measurable")])).toBeNull();
    expect(areaScore([])).toBeNull();
  });

  it("rounds to two decimals like the evaluator", () => {
    expect(areaScore([check("a", "passed"), check("a", "failed"), check("a", "failed")])).toBe(33.33);
  });
});

describe("areaScore weights", () => {
  const weighted = (outcome: CheckOutcome, weight: number, checkId: string) => ({ ...check("google", outcome, checkId), weight });

  it("weights every check by its own weight like the evaluator", () => {
    expect(areaScore([weighted("passed", 3, "a2"), weighted("failed", 1, "a4"), weighted("partial", 4, "a7")])).toBe(62.5);
  });

  it("lets the heavier check move the score further", () => {
    expect(areaScore([weighted("passed", 4.329, "a2"), weighted("failed", 1.623, "a12")])).toBe(72.73);
    expect(areaScore([weighted("failed", 4.329, "a2"), weighted("passed", 1.623, "a12")])).toBe(27.27);
  });

  it("falls back to the plain mean when every weight is zero", () => {
    expect(areaScore([weighted("passed", 0, "a2"), weighted("failed", 0, "a4")])).toBe(50);
  });

  it("leaves unmeasured checks out of the weights", () => {
    expect(areaScore([weighted("passed", 3, "a2"), weighted("failed", 1, "a4"), weighted("not_measurable", 40, "a7")])).toBe(75);
  });
});

describe("areaScore and the control verdicts", () => {
  it("leaves a check out of the share when one of its boxes failed", () => {
    const excluded = { ...check("google", "failed", "a2"), verdict_data_ok: false, verdict_data_reason: "Zahl stimmt nicht" };
    expect(areaScore([check("google", "passed", "a4"), excluded])).toBe(100);
    expect(areaScore([excluded])).toBeNull();
  });

  it("keeps a check in the share when all three boxes are ok", () => {
    const sound = { ...check("google", "failed", "a2"), verdict_collected_ok: true, verdict_data_ok: true, verdict_evaluation_ok: true };
    expect(areaScore([check("google", "passed", "a4"), sound])).toBe(50);
  });
});

describe("scoreTone", () => {
  it("maps scores to traffic-light tones", () => {
    expect(scoreTone(80)).toBe("ok");
    expect(scoreTone(70)).toBe("ok");
    expect(scoreTone(69.99)).toBe("warn");
    expect(scoreTone(40)).toBe("warn");
    expect(scoreTone(39.99)).toBe("bad");
    expect(scoreTone(null)).toBe("none");
  });
});

describe("groupByArea", () => {
  it("groups in catalogue order with German names and counts", () => {
    const areas = groupByArea([check("bing", "failed", "a14"), check("google", "passed", "a2"), check("google", "partial", "a4"), check("bing", "not_measurable", "b10")]);
    expect(areas.map((area) => area.area)).toEqual(["google", "bing"]);
    expect(areas[0]).toMatchObject({ name: "Google", score: 75, tone: "ok" });
    expect(areas[1]).toMatchObject({ name: "Bing und Copilot", score: 0, tone: "bad" });
    expect(areas[1]?.counts).toEqual({ passed: 0, partial: 0, failed: 1, not_measurable: 1, skipped: 0, not_applicable: 0 });
  });

  it("keeps unknown areas at the end with a fallback name", () => {
    const areas = groupByArea([check("z", "passed"), check("apple", "passed", "h1")]);
    expect(areas.map((area) => area.name)).toEqual(["Apple", "Bereich z"]);
  });

  it("gives no total value for the information checks of the paid area", () => {
    expect(groupByArea([check("ads", "passed", "p1"), check("ads", "failed", "p3")])).toEqual([]);
  });

  it("counts not applicable checks apart and leaves them out of the share", () => {
    const [area] = groupByArea([check("google", "passed", "a2"), check("google", "not_applicable", "j2")]);
    expect(area?.score).toBe(100);
    expect(area?.counts.not_applicable).toBe(1);
  });

  it("returns nothing for no checks", () => {
    expect(groupByArea([])).toEqual([]);
  });
});

describe("sortFindings", () => {
  it("puts the most severe first and orders checks naturally within a severity", () => {
    const sorted = sortFindings([
      finding({ severity: 1, check_id: "a1" }),
      finding({ severity: 3, check_id: "b10" }),
      finding({ severity: 3, check_id: "b2" }),
      finding({ severity: 2, check_id: "c1" }),
    ]);
    expect(sorted.map((item) => item.check_id)).toEqual(["b2", "b10", "c1", "a1"]);
  });
});

describe("scoreHistory", () => {
  it("lists scored audits oldest first and skips unscored ones", () => {
    const history = scoreHistory([
      audit({ total_score: 72, finished_at: "2026-10-02T10:00:00Z" }),
      audit({ total_score: null }),
      audit({ total_score: 55, finished_at: null, requested_at: "2026-09-01T10:00:00Z" }),
    ]);
    expect(history).toEqual([
      { label: "01.09.2026", score: 55 },
      { label: "02.10.2026", score: 72 },
    ]);
  });
});
