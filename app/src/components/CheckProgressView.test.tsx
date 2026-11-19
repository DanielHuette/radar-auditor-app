// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { CheckOutcome, CheckResult } from "../api/types";
import { buildRows } from "../lib/evaluation";
import { check, EMPTY_CONTEXT } from "../lib/testRows";
import { CheckRow } from "./CheckRow";
import { JobStatusBadge } from "./JobStatusBadge";
import { NOT_COUNTED_TEXT } from "./CheckProgressView";

let root: HTMLDivElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.append(root);
});

afterEach(() => {
  render(null, root);
  root.remove();
});

function result(outcome: CheckOutcome, overrides: Partial<CheckResult> = {}, checkId = "a2"): CheckResult {
  return { ...check("google", outcome, checkId), ...overrides };
}

async function show(checkResult: CheckResult, context = EMPTY_CONTEXT) {
  const [row] = buildRows([checkResult], []);
  if (row === undefined) throw new Error("no row");
  await act(() => {
    render(
      <ul>
        <CheckRow row={row} context={context} />
      </ul>,
      root,
    );
  });
}

const boxes = () => [...root.querySelectorAll(".cluster[role='group'] > *")];
const boxText = (index: number) => {
  const box = boxes()[index];
  return box?.querySelector("summary")?.textContent ?? box?.textContent ?? "";
};
const bar = () => root.querySelector("[role='progressbar']");

describe("progress bar of a check", () => {
  it("shows the stage as text and as progressbar value", async () => {
    await show(result("failed"));
    expect(bar()?.getAttribute("aria-valuetext")).toBe("Kontrolle");
    expect(bar()?.getAttribute("aria-valuenow")).toBe("80");
  });

  it("shows the waiting control with attempt and reason", async () => {
    await show(result("failed", { control_attempt: 2, control_last_error: "Agent nicht erreichbar" }));
    expect(bar()?.getAttribute("aria-valuetext")).toBe("Kontrolle wartet – Versuch 2: Agent nicht erreichbar");
    expect(root.textContent).toContain("Kontrolle wartet – Versuch 2: Agent nicht erreichbar");
  });

  it("is full once the control is done", async () => {
    await show(result("passed", { control_state: "done", verdict_collected_ok: true, verdict_data_ok: true, verdict_evaluation_ok: true }));
    expect(bar()?.getAttribute("aria-valuetext")).toBe("fertig");
    expect(bar()?.getAttribute("aria-valuenow")).toBe("100");
  });
});

describe("the three boxes", () => {
  it("are empty while the control is open", async () => {
    await show(result("failed"));
    expect(boxes()).toHaveLength(3);
    expect(boxes().map((box) => box.textContent)).toEqual([
      "☐ Daten korrekt eingeholt: noch offen",
      "☐ Daten korrekt: noch offen",
      "☐ Auswertung korrekt: noch offen",
    ]);
  });

  it("show a check each when all three verdicts are ok", async () => {
    await show(result("passed", { control_state: "done", verdict_collected_ok: true, verdict_data_ok: true, verdict_evaluation_ok: true }));
    expect(boxes().map((box) => box.textContent)).toEqual([
      "☑ Daten korrekt eingeholt: Haken",
      "☑ Daten korrekt: Haken",
      "☑ Auswertung korrekt: Haken",
    ]);
    expect(root.querySelector(".tone-bad")).toBeNull();
    expect(root.textContent).not.toContain(NOT_COUNTED_TEXT);
  });

  it("show a cross with the plain reason and the evidence, and say the check does not count", async () => {
    await show(
      result("failed", {
        control_state: "done",
        verdict_collected_ok: true,
        verdict_data_ok: false,
        verdict_data_reason: "Die Zahl steht so nicht in den Rohdaten.",
        verdict_data_evidence: "cls: 0,4",
        verdict_evaluation_ok: true,
      }),
    );
    expect(boxText(1)).toBe("☒ Daten korrekt: X");
    const detail = root.querySelector(".cluster[role='group'] details");
    expect(detail?.querySelector("summary")?.getAttribute("title")).toContain("Die Zahl steht so nicht in den Rohdaten.");
    expect(detail?.textContent).toContain("Die Zahl steht so nicht in den Rohdaten.");
    expect(detail?.textContent).toContain("Belegstelle: cls: 0,4");
    expect(root.textContent).toContain(NOT_COUNTED_TEXT);
  });

  it("keep boxes 2 and 3 empty when box 1 is a cross", async () => {
    await show(
      result("passed", {
        control_state: "done",
        verdict_collected_ok: false,
        verdict_collected_reason: "Die Seite war beim Abruf leer.",
        verdict_data_ok: true,
        verdict_evaluation_ok: true,
      }),
    );
    expect(boxText(0)).toBe("☒ Daten korrekt eingeholt: X");
    expect(boxText(1)).toBe("☐ Daten korrekt: noch offen");
    expect(boxText(2)).toBe("☐ Auswertung korrekt: noch offen");
    expect(root.textContent).toContain("Die Seite war beim Abruf leer.");
  });

  it("can be opened with the keyboard through a native summary", async () => {
    await show(result("failed", { control_state: "done", verdict_evaluation_ok: false, verdict_evaluation_reason: "Schwelle falsch" }));
    const summary = root.querySelector(".cluster[role='group'] details summary");
    expect(summary?.tagName).toBe("SUMMARY");
  });
});

describe("check kinds and states", () => {
  it("shows an information check as a pure statement without a rating", async () => {
    await show(result("failed", {}, "p3"));
    expect(root.querySelector(".check__meta .badge")?.textContent).toBe("Feststellung");
    expect(root.querySelector(".check__meta .tone-bad")).toBeNull();
  });

  it("shows the official source of a criterion as a link", async () => {
    await show(result("passed", {}, "a2"));
    const link = root.querySelector(".check__body a");
    expect(link?.getAttribute("href")).toBe("https://developers.google.com/search/docs/appearance/page-experience");
  });

  it("names a not measured check with its reason and shows no bar", async () => {
    await show(result("not_measurable", { measured: { reason: "no_website" } }));
    expect(root.querySelector(".check__meta .badge")?.textContent).toBe("nicht gemessen");
    expect(root.querySelector(".check__reason")?.textContent).toBe("Nicht gemessen: Keine Website gefunden oder nicht erreichbar.");
  });

  it("names a not applicable check without a location", async () => {
    await show(result("not_applicable", { measured: { reason: "no_location" } }, "j3"));
    expect(root.querySelector(".check__meta .badge")?.textContent).toBe("nicht zutreffend");
    expect(root.querySelector(".check__reason")?.textContent).toBe("Nicht zutreffend: Der Betrieb hat keinen Standort.");
    expect(bar()).toBeNull();
    expect(boxes()).toHaveLength(0);
  });
});

describe("JobStatusBadge", () => {
  it("says the control is running for the audit status controlling", async () => {
    await act(() => {
      render(<JobStatusBadge status="controlling" />, root);
    });
    expect(root.textContent).toBe("Kontrolle läuft");
  });

  it("says the control is running for a scored audit while verdicts are missing", async () => {
    await act(() => {
      render(<JobStatusBadge status="scored" controlRunning />, root);
    });
    expect(root.textContent).toBe("Kontrolle läuft");
  });

  it("keeps the plain label once the verdicts are in", async () => {
    await act(() => {
      render(<JobStatusBadge status="scored" controlRunning={false} />, root);
    });
    expect(root.textContent).toBe("Bewertet");
  });
});
