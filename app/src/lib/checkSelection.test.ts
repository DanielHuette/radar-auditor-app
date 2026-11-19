import { afterEach, describe, expect, it, vi } from "vitest";
import { CATALOGUE_AREAS, type CatalogueArea } from "./catalogue";
import {
  AREA_ORDER,
  NO_RATING_TAG,
  SELECTION_STORAGE_KEY,
  areaState,
  areaTags,
  checkTags,
  defaultDisabled,
  deviceStorage,
  enabledCount,
  loadSelection,
  orderedAreas,
  saveSelection,
  setAll,
  setArea,
  setCheck,
} from "./checkSelection";
import { catalogueCheck as check } from "./testRows";

const g: CatalogueArea = { id: "google", name: "Google", weight: 10, checks: [check("g1"), check("g2")] };
const m: CatalogueArea = {
  id: "ads",
  name: "Bezahlte Sichtbarkeit",
  weight: 0,
  checks: [check("m1", { requiresAccess: true }), check("m2", { requiresAccess: true })],
};

function memoryStorage(initial: Record<string, string> = {}) {
  const items = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => {
      items.set(key, value);
    },
    items,
  };
}

const throwing = {
  getItem: (): string | null => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("blocked");
  },
};

describe("the catalogue on the Prüfen page", () => {
  it("lists the six providers in the agreed order with 62 checks", () => {
    expect(orderedAreas().map((area) => area.id)).toEqual(["google", "bing", "apple", "openai", "anthropic", "ads"]);
    expect(orderedAreas().map((area) => area.id)).toEqual(AREA_ORDER);
    expect(enabledCount([], CATALOGUE_AREAS).total).toBe(62);
  });

  it("gives every check a name and a reason", () => {
    const checks = CATALOGUE_AREAS.flatMap((area) => area.checks);
    expect(checks.every((item) => item.name.trim() !== "" && item.why.trim() !== "")).toBe(true);
    expect(new Set(checks.map((item) => item.id)).size).toBe(checks.length);
  });

  it("puts areas missing from the order after the known ones", () => {
    const z: CatalogueArea = { id: "z", name: "Neu", weight: 1, checks: [check("z1")] };
    expect(orderedAreas([z, m, g]).map((area) => area.id)).toEqual(["google", "ads", "z"]);
  });
});

describe("tags", () => {
  const byId = (id: string) => CATALOGUE_AREAS.find((area) => area.id === id);

  it("marks paid checks and the customer-access checks of an area", () => {
    expect(areaTags(byId("google") ?? g)).toEqual([{ label: "3 kosten Geld" }, { label: "braucht Kundenzugang" }]);
    expect(areaTags(byId("ads") ?? g)).toEqual([{ label: "2 kosten Geld" }, { label: "braucht Kundenzugang" }]);
    expect(areaTags(byId("bing") ?? g)).toEqual([]);
  });

  it("marks information checks as unrated and paid checks as costly", () => {
    const checks = new Map(CATALOGUE_AREAS.flatMap((area) => area.checks.map((item) => [item.id, item] as const)));
    expect(checkTags(checks.get("p1") ?? check("p1"))).toEqual([{ label: NO_RATING_TAG }, { label: "kostet Geld" }]);
    expect(checkTags(checks.get("p3") ?? check("p3"))).toEqual([{ label: NO_RATING_TAG }]);
    expect(checkTags(checks.get("c12") ?? check("c12"))).toEqual([{ label: "kostet Geld" }]);
    expect(checkTags(checks.get("a2") ?? check("a2"))).toEqual([]);
  });
});

describe("switching checks", () => {
  it("switches one check, one area and everything", () => {
    expect(setCheck([], "g1", false)).toEqual(["g1"]);
    expect(setCheck(["g1"], "g1", true)).toEqual([]);
    expect(setArea(["m1"], g, false)).toEqual(["g1", "g2", "m1"]);
    expect(setArea(["g1", "g2", "m1"], g, true)).toEqual(["m1"]);
    expect(setAll([g, m], false)).toEqual(["g1", "g2", "m1", "m2"]);
    expect(setAll([g, m], true)).toEqual([]);
  });

  it("tells whether an area is fully, partly or not on", () => {
    expect(areaState([], g)).toBe("all");
    expect(areaState(["g1"], g)).toBe("some");
    expect(areaState(["g1", "g2"], g)).toBe("none");
  });

  it("counts enabled checks; ids no longer in the catalogue do not count", () => {
    expect(enabledCount(["g1", "x9"], [g, m])).toEqual({ enabled: 3, total: 4 });
  });
});

describe("default and remembered selection", () => {
  it("switches on everything except the customer-access area by default", () => {
    expect(defaultDisabled([g, m])).toEqual(["m1", "m2"]);
    expect(defaultDisabled()).toEqual(["m1", "m2", "p5", "p6"]);
    expect(enabledCount(defaultDisabled(), CATALOGUE_AREAS)).toEqual({ enabled: 58, total: 62 });
  });

  it("uses the default without storage, with nothing stored or with unreadable data", () => {
    expect(loadSelection(null, [g, m])).toEqual(["m1", "m2"]);
    expect(loadSelection(memoryStorage(), [g, m])).toEqual(["m1", "m2"]);
    expect(loadSelection(memoryStorage({ [SELECTION_STORAGE_KEY]: "{kaputt" }), [g, m])).toEqual(["m1", "m2"]);
    expect(loadSelection(memoryStorage({ [SELECTION_STORAGE_KEY]: '{"g1":true}' }), [g, m])).toEqual(["m1", "m2"]);
    expect(loadSelection(throwing, [g, m])).toEqual(["m1", "m2"]);
  });

  it("brings back the last selection, without ids that left the catalogue", () => {
    const storage = memoryStorage();
    saveSelection(storage, ["g2", "x9"]);
    expect(storage.items.get(SELECTION_STORAGE_KEY)).toBe('["g2","x9"]');
    expect(loadSelection(storage, [g, m])).toEqual(["g2"]);
    saveSelection(storage, []);
    expect(loadSelection(storage, [g, m])).toEqual([]);
  });

  it("keeps working when storage refuses to save", () => {
    expect(() => {
      saveSelection(throwing, ["g1"]);
    }).not.toThrow();
    expect(() => {
      saveSelection(null, ["g1"]);
    }).not.toThrow();
  });
});

describe("deviceStorage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is the localStorage of the browser", () => {
    const storage = { getItem: () => null, setItem: () => undefined };
    vi.stubGlobal("window", { localStorage: storage });
    expect(deviceStorage()).toBe(storage);
  });

  it("is null where reading localStorage throws", () => {
    vi.stubGlobal("window", {
      get localStorage(): Storage {
        throw new DOMException("blocked", "SecurityError");
      },
    });
    expect(deviceStorage()).toBeNull();
  });
});
