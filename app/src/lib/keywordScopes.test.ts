import { describe, expect, it } from "vitest";
import type { Keyword, Ranking } from "../api/types";
import { keywordScopes } from "./keywordScopes";

function keyword(id: string, phrase: string, scope: Keyword["scope"], volume: number | null): Keyword {
  return { id, customer_id: "c1", phrase, city: "Münster", monthly_volume: volume, is_primary: false, scope };
}

function ranking(keywordId: string, subject: string, organic: number | null, local: number | null, grid: string | null = null): Ranking {
  return {
    id: `${keywordId}-${subject}-${String(organic)}-${String(grid)}`,
    audit_id: "a1",
    keyword_id: keywordId,
    subject,
    organic_position: organic,
    local_pack_position: local,
    grid_point: grid,
    measured_at: "2026-11-13T08:00:00Z",
  };
}

const KEYWORDS = [
  keyword("k1", "Malerin Münster", "local", 10),
  keyword("k2", "Kunstkurs", "nationwide", 500),
  keyword("k3", "Atelier Münster", "local", 90),
  keyword("k4", "Acryl lernen", "nationwide", null),
];

describe("keywordScopes", () => {
  it("separates local from nationwide keywords and sorts each by demand", () => {
    const [local, nationwide] = keywordScopes(KEYWORDS, [], "Katja Karsch");
    expect(local?.title).toBe("Örtliche Suchbegriffe");
    expect(local?.lines.map((line) => line.phrase)).toEqual(["Atelier Münster", "Malerin Münster"]);
    expect(nationwide?.title).toBe("Bundesweite Suchbegriffe");
    expect(nationwide?.lines.map((line) => line.phrase)).toEqual(["Kunstkurs", "Acryl lernen"]);
  });

  it("takes the best own position and ignores competitors and grid points", () => {
    const rankings = [
      ranking("k1", "Katja Karsch", 7, 3),
      ranking("k1", "Katja Karsch", 4, null),
      ranking("k1", "Katja Karsch", 1, 1, "2:3"),
      ranking("k1", "Atelier Konkurrenz", 1, 1),
    ];
    const line = keywordScopes(KEYWORDS, rankings, "katja karsch")[0]?.lines.find((entry) => entry.id === "k1");
    expect(line).toMatchObject({ measured: true, organic: 4, local: 3 });
  });

  it("marks a keyword as measured when only competitors were found, with no own position", () => {
    const line = keywordScopes(KEYWORDS, [ranking("k2", "Anderer", 1, null)], "Katja Karsch")[1]?.lines.find((entry) => entry.id === "k2");
    expect(line).toMatchObject({ measured: true, organic: null, local: null });
  });

  it("marks a keyword without any ranking as not measured", () => {
    const line = keywordScopes(KEYWORDS, [], "Katja Karsch")[1]?.lines.find((entry) => entry.id === "k4");
    expect(line).toMatchObject({ measured: false, volume: null });
  });

  it("returns both groups even when one has no keyword", () => {
    const groups = keywordScopes([keyword("k1", "Malerin Münster", "local", 10)], [], "Katja Karsch");
    expect(groups.map((group) => group.lines.length)).toEqual([1, 0]);
  });
});
