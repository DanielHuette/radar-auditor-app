// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { describe, expect, it, vi } from "vitest";
import { SOURCE_IDS } from "../lib/sources";
import { Settings } from "./Settings";

const getSetting = vi.fn<(key: string) => Promise<unknown>>();

vi.mock("../api/supabase", () => ({ supabase: {} }));
vi.mock("../api/queries", () => ({
  getSetting: (key: string) => getSetting(key),
  saveSetting: () => Promise.resolve(),
  sourceUsageSince: () => Promise.resolve([]),
}));

describe("Settings", () => {
  it("keeps sources and cost caps and has no check toggles", async () => {
    getSetting.mockResolvedValue(null);
    const root = document.createElement("div");
    await act(() => {
      render(<Settings />, root);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(getSetting.mock.calls.map(([key]) => key)).toEqual(["sources"]);
    expect([...root.querySelectorAll("h2")].map((heading) => heading.textContent)).toEqual(["Datenquellen", "App installieren"]);
    expect(root.querySelectorAll('input[type="checkbox"]:not([role="switch"])')).toHaveLength(0);
    expect(root.querySelectorAll('[role="switch"]').length).toBeGreaterThan(0);
    expect(root.textContent).toContain("Monatslimit");
    expect(root.textContent).toContain("Für jede Messung gibt es eine feste Reihenfolge der Wege");
    expect(root.textContent).toContain("Erster von drei Wegen für Google-Profil und Karte. Die Google Places API ist");
    expect(root.textContent).not.toContain("im Browser auf dem Server abgelesen");
    expect(root.querySelectorAll(".source")).toHaveLength(SOURCE_IDS.length);
    render(null, root);
  });

  it("never leaves the install card empty", async () => {
    getSetting.mockResolvedValue(null);
    const root = document.createElement("div");
    await act(() => {
      render(<Settings />, root);
    });
    const card = root.querySelector(".settings-install");
    expect(card?.querySelector("p, button")?.textContent.length).toBeGreaterThan(0);
    render(null, root);
  });
});
