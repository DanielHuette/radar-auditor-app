import { describe, expect, it } from "vitest";
import { estimateFileName, reportFileName } from "./reportFile";

describe("reportFileName", () => {
  it("joins customer and day with dashes and keeps umlauts", () => {
    expect(reportFileName("Müller & Söhne GmbH", "2026-11-13T05:45:53+00:00")).toBe("Audit-Müller-Söhne-GmbH-2026-11-13.pdf");
  });

  it("falls back to a plain name when the customer has no usable letters", () => {
    expect(reportFileName(" / ", "2026-11-13T05:45:53+00:00")).toBe("Audit-2026-11-13.pdf");
  });
});

describe("estimateFileName", () => {
  it("names the estimate after customer and day", () => {
    expect(estimateFileName("Katja Karsch", "2026-11-13T07:38:32+00:00")).toBe("Kostenvoranschlag-Katja-Karsch-2026-11-13.pdf");
    expect(estimateFileName("", "2026-11-13T07:38:32+00:00")).toBe("Kostenvoranschlag-2026-11-13.pdf");
  });
});
