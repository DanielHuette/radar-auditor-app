import { describe, expect, it } from "vitest";
import { comparisonText } from "./comparison";

describe("comparisonText", () => {
  it("names the own value and the strongest competitor of the same audit", () => {
    const measured = {
      mentioned: 3,
      answers: 60,
      competitors: [
        { name: "Dach GmbH", mentioned: 22, answers: 60 },
        { name: "Zweit", mentioned: 5, answers: 60 },
      ],
    };

    expect(comparisonText({ checkId: "k1", measured })).toBe(
      "3 von 60 Antworten nennen den Betrieb; stärkster Wettbewerber (Dach GmbH): 22 von 60",
    );
  });

  it("says plainly that no competitor values exist", () => {
    expect(comparisonText({ checkId: "k2", measured: { mentioned: 1, answers: 4, competitors: [] } })).toBe(
      "1 von 4 Antworten nennen den Betrieb; Wettbewerberwerte liegen nicht vor",
    );
  });

  it("shows the grid without a verdict and without invented competitor values", () => {
    const measured = { points_in_top: 4, points_total: 25, top: 3, competitor_values: null };

    expect(comparisonText({ checkId: "j3", measured })).toBe(
      "4 von 25 Rasterpunkten in den ersten 3; Wettbewerberwerte liegen nicht vor",
    );
  });

  it("shows nothing for a check without such a measurement", () => {
    expect(comparisonText({ checkId: "a2", measured: {} })).toBeNull();
  });
});