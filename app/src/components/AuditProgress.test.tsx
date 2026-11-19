// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuditProgressState } from "../hooks/auditProgress";
import { MANAGER_VERDICT_LABEL } from "../lib/progress";
import { AuditProgress } from "./AuditProgress";

let state: AuditProgressState;

vi.mock("../api/supabase", () => ({ supabase: {} }));
vi.mock("../hooks/auditProgress", () => ({ useAuditProgress: () => state }));

let root: HTMLDivElement;

function loaded(manager: AuditProgressState["manager"]): AuditProgressState {
  return {
    audit: {
      id: "a1",
      customer_id: "c1",
      kind: "remote",
      status: "reported",
      requested_at: "2026-11-13T08:00:00Z",
      finished_at: null,
      total_score: 61,
      report_path: null,
      error: null,
      customers: { name: "Katja Karsch" },
    },
    progress: { step: "done", serpRetry: false, analysisWait: null, error: null },
    manager,
    routes: [],
    repairs: [],
    checks: [],
    states: new Map(),
    eventReasons: new Map(),
    checksScored: 0,
    estimates: [],
    error: null,
  };
}

async function mount() {
  await act(() => {
    render(<AuditProgress auditId="a1" onNew={() => undefined} />, root);
  });
}

beforeEach(() => {
  root = document.createElement("div");
  document.body.append(root);
});

afterEach(() => {
  render(null, root);
  root.remove();
});

describe("AuditProgress manager line", () => {
  it("shows the verdict of the audit manager", async () => {
    state = loaded([{ occurred_at: "2026-11-13T09:00:00Z", result: "gemessen", label: MANAGER_VERDICT_LABEL, reason: "Alles gemessen" }]);
    await mount();
    expect(root.textContent).toContain("Audit-Manager: vollständig und plausibel – Alles gemessen");
    expect(root.querySelector(".notice--bad")).toBeNull();
  });

  it("shows a failed verdict as an alert", async () => {
    state = loaded([{ occurred_at: "2026-11-13T09:00:00Z", result: "fehlgeschlagen", label: MANAGER_VERDICT_LABEL, reason: "Bing fehlt" }]);
    await mount();
    expect(root.querySelector(".notice--bad")?.textContent).toBe("Audit-Manager: fehlgeschlagen – Bing fehlt");
  });

  it("shows nothing of the manager before it wrote anything", async () => {
    state = loaded([]);
    await mount();
    expect(root.textContent).not.toContain("Audit-Manager");
  });
});

describe("AuditProgress routes", () => {
  it("lists every measuring route with its result and reason", async () => {
    state = {
      ...loaded([]),
      routes: [
        { key: "assistant:claude", label: "Claude", result: "fehlgeschlagen", reason: "Not logged in", stage: 1 },
        { key: "google_search", label: "Google-Suche", result: "gemessen", reason: null, stage: 1 },
      ],
      checksScored: 3,
    };
    await mount();
    const rows = [...root.querySelectorAll(".routes:not(.routes--checks) .route")].map((row) => row.textContent);
    expect(rows).toEqual(["ClaudefehlgeschlagenNot logged in", "Google-Suchegemessen"]);
    expect(root.textContent).toContain("3 Prüfungen bewertet");
  });

  it("names a halted audit with its reason instead of the steps", async () => {
    state = { ...loaded([]), progress: { step: "halted", serpRetry: false, analysisWait: null, error: "Claude nicht angemeldet" } };
    await mount();
    expect(root.textContent).toContain("Angehalten: Claude nicht angemeldet");
    expect(root.querySelector(".steps")).toBeNull();
  });

  it("lists every restart and takeover of the audit agent", async () => {
    state = {
      ...loaded([]),
      repairs: [
        { key: "r1", time: "2026-11-13T09:00:00Z", label: "Audit-Agent übernimmt", result: "neu gestartet", reason: "Bing-Suche, Versuch 1: Bing zeigt eine Sicherheitsabfrage" },
        { key: "r2", time: "2026-11-13T09:05:00Z", label: "Audit-Agent: Teil erledigt", result: "gemessen", reason: "Bing-Suche nach Versuch 1 gemessen" },
      ],
    };
    await mount();
    expect(root.textContent).toContain("Neustarts und Übernahmen durch den Audit-Agenten");
    const rows = [...root.querySelectorAll(".routes:not(.routes--checks) .route")].map((row) => row.textContent);
    expect(rows[0]).toContain("Audit-Agent übernimmtneu gestartetBing-Suche, Versuch 1: Bing zeigt eine Sicherheitsabfrage");
    expect(rows[1]).toContain("Audit-Agent: Teil erledigtgemessenBing-Suche nach Versuch 1 gemessen");
  });

  it("shows no measurement list before the first event", async () => {
    state = loaded([]);
    await mount();
    expect(root.querySelector(".routes:not(.routes--checks)")).toBeNull();
  });
});
