// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { LocationProvider } from "preact-iso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Customer, OpenAuditRequest } from "../api/types";
import { AdminContext } from "../hooks/role";
import { SELECTION_STORAGE_KEY } from "../lib/checkSelection";
import { Pruefen } from "./Pruefen";

const startFieldAudit = vi.fn<(request: unknown) => Promise<string>>();
const listOpenAuditRequests = vi.fn<() => Promise<OpenAuditRequest[]>>();
const listCustomers = vi.fn<() => Promise<Customer[]>>();
const startCustomerAudit = vi.fn<(customerId: string, disabled: string[]) => Promise<string>>();

vi.mock("../api/supabase", () => ({ supabase: {} }));
vi.mock("../api/queries", () => ({
  startFieldAudit: (request: unknown) => startFieldAudit(request),
  listOpenAuditRequests: () => listOpenAuditRequests(),
  listCustomers: () => listCustomers(),
  startCustomerAudit: (customerId: string, disabled: string[]) => startCustomerAudit(customerId, disabled),
  getHeartbeat: () => Promise.resolve(null),
  getAuditProgress: () => new Promise(() => undefined),
}));
vi.mock("../hooks/auditProgress", () => ({ useAuditProgress: () => ({ audit: undefined, progress: null, manager: [], routes: [], checksScored: 0, estimates: [], error: null }) }));

let root: HTMLDivElement;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function mount(admin?: boolean) {
  await act(() => {
    render(
      <AdminContext.Provider value={admin}>
        <LocationProvider>
          <Pruefen />
        </LocationProvider>
      </AdminContext.Provider>,
      root,
    );
  });
  await flush();
}

async function type(value: string) {
  const input = root.querySelector<HTMLInputElement>(".url-field__input");
  if (input === null) throw new Error("no address field");
  await act(() => {
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submit(selector: string) {
  const form = root.querySelector(selector);
  if (form === null) throw new Error(`no ${selector}`);
  await act(() => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
}

beforeEach(() => {
  startFieldAudit.mockReset();
  listOpenAuditRequests.mockReset();
  listOpenAuditRequests.mockResolvedValue([]);
  listCustomers.mockReset();
  listCustomers.mockResolvedValue([]);
  startCustomerAudit.mockReset();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  render(null, root);
  root.remove();
  vi.restoreAllMocks();
});

describe("Prüfen, step 1", () => {
  it("shows only the address field and the button", async () => {
    await mount(true);
    expect(root.querySelectorAll("input")).toHaveLength(1);
    expect([...root.querySelectorAll("button")].map((button) => button.textContent)).toEqual(["Prüfen"]);
    expect(root.querySelector("h1, h2")).toBeNull();
  });

  it("names an invalid address and stays on step 1", async () => {
    await mount();
    await type("muster");
    await submit(".start");
    expect(root.querySelector('[role="alert"]')?.textContent).toBe("Bitte eine Adresse wie meisterbetrieb-muster.de eingeben.");
    expect(root.querySelector(".selection")).toBeNull();
  });
});

describe("Prüfen, step 2", () => {
  it("lists the six providers with the address and starts with only website and switched-off checks", async () => {
    startFieldAudit.mockResolvedValue("audit-9");
    await mount();
    await type("Dachdeckerei-Muster.de");
    await submit(".start");
    expect(root.querySelector(".address-label")?.textContent).toBe("dachdeckerei-muster.de");
    expect(root.querySelectorAll(".sel-area")).toHaveLength(6);
    expect(root.querySelector(".sel-row--all .sel-row__count")?.textContent).toBe("58 von 62");
    await submit(".choose");
    expect(startFieldAudit).toHaveBeenCalledWith({
      p_website: "https://dachdeckerei-muster.de/",
      p_disabled_checks: ["m1", "m2", "p5", "p6"],
    });
    expect(window.location.search).toBe("?pruefung=audit-9");
  });

  it("remembers the selection on this device", async () => {
    await mount();
    await type("muster.de");
    await submit(".start");
    const bing = root.querySelector<HTMLInputElement>(".sel-area:nth-child(2) input");
    if (bing === null) throw new Error("no area checkbox");
    await act(() => {
      bing.click();
    });
    const stored = JSON.parse(localStorage.getItem(SELECTION_STORAGE_KEY) ?? "[]") as string[];
    expect(stored).toHaveLength(22);
    expect(stored).toContain("a14");
    expect(root.querySelector(".sel-row--all .sel-row__count")?.textContent).toBe("40 von 62");
  });

  it("works with the default selection when the browser refuses storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    startFieldAudit.mockResolvedValue("audit-10");
    await mount();
    await type("muster.de");
    await submit(".start");
    expect(root.querySelector(".sel-row--all .sel-row__count")?.textContent).toBe("58 von 62");
    await submit(".choose");
    expect(startFieldAudit).toHaveBeenCalledWith({ p_website: "https://muster.de/", p_disabled_checks: ["m1", "m2", "p5", "p6"] });
  });

  it("opens an area with its chevron and shows each check with its reason", async () => {
    await mount();
    await type("muster.de");
    await submit(".start");
    const chevron = root.querySelector<HTMLButtonElement>(".sel-area:nth-child(6) .sel-row__open");
    if (chevron === null) throw new Error("no chevron");
    await act(() => {
      chevron.click();
    });
    expect(chevron.getAttribute("aria-expanded")).toBe("true");
    const checks = root.querySelectorAll(".sel-area:nth-child(6) .sel-check");
    expect(checks).toHaveLength(6);
    expect(checks[0]?.querySelector(".tag")?.textContent).toBe("ohne Wertung");
    expect(checks[0]?.querySelector(".sel-check__why")?.textContent).not.toBe("");
  });
});

function openRequest(overrides: Partial<OpenAuditRequest>): OpenAuditRequest {
  return {
    id: "r1",
    customer_id: null,
    company: "Dachdeckerei Muster",
    contact: "Erika Muster",
    website: "https://dachdeckerei-muster.de/",
    kind: "visit",
    wished_at: "2026-11-03T08:00:00+00:00",
    status: "open",
    created_at: "2026-10-30T10:00:00+00:00",
    ...overrides,
  };
}

const TWO_REQUESTS = [
  openRequest({ id: "ohne", company: "Malerei Beispiel", contact: "Max Beispiel", website: null, kind: "call", wished_at: "2026-11-04T09:00:00Z" }),
  openRequest({}),
];

describe("Prüfen, open requests", () => {
  it("asks nobody but the administrator for them", async () => {
    await mount(false);
    expect(listOpenAuditRequests).not.toHaveBeenCalled();
    expect(root.querySelector(".requests")).toBeNull();
  });

  it("shows a closed list below the button, earliest wish first", async () => {
    listOpenAuditRequests.mockResolvedValue(TWO_REQUESTS);
    await mount(true);
    const list = root.querySelector<HTMLDetailsElement>("details.requests");
    expect(list?.open).toBe(false);
    expect(list?.querySelector("summary")?.textContent).toBe("2 offene Anfragen");
    const rows = [...root.querySelectorAll(".request")];
    expect(rows.map((row) => row.querySelector(".request__title")?.textContent)).toEqual(["Dachdeckerei Muster", "Malerei Beispiel"]);
    expect(rows[0]?.textContent).toContain("Erika Muster");
    expect(rows[0]?.textContent).toContain("Vor Ort");
    expect(rows[0]?.textContent).toContain("Dienstag, 03.11.2026, 09:00 Uhr");
    expect(rows[0]?.textContent).toContain("dachdeckerei-muster.de");
  });

  it("offers no check for a request without a website", async () => {
    listOpenAuditRequests.mockResolvedValue(TWO_REQUESTS);
    await mount(true);
    const without = [...root.querySelectorAll(".request")][1];
    expect(without?.tagName).not.toBe("BUTTON");
    expect(without?.querySelector("button")).toBeNull();
    expect(without?.textContent).toContain("Keine Webseite angegeben");
  });

  it("puts the website of a tapped request into the address field, and Prüfen goes on as usual", async () => {
    listOpenAuditRequests.mockResolvedValue(TWO_REQUESTS);
    await mount(true);
    const list = root.querySelector<HTMLDetailsElement>("details.requests");
    if (list === null) throw new Error("no request list");
    list.open = true;
    const tap = root.querySelector<HTMLButtonElement>("button.request");
    if (tap === null) throw new Error("no request button");
    await act(() => {
      tap.click();
    });
    await flush();
    expect(root.querySelector<HTMLInputElement>(".url-field__input")?.value).toBe("https://dachdeckerei-muster.de/");
    expect(root.querySelector<HTMLDetailsElement>("details.requests")?.open).toBe(false);
    expect(document.activeElement?.textContent).toBe("Prüfen");
    expect(startFieldAudit).not.toHaveBeenCalled();
    await submit(".start");
    expect(root.querySelector(".address-label")?.textContent).toBe("dachdeckerei-muster.de");
  });

  it("names a failed load inside the closed list instead of hiding it", async () => {
    listOpenAuditRequests.mockRejectedValue(new Error("boom"));
    await mount(true);
    expect(root.querySelector("details.requests summary")?.textContent).toBe("Offene Anfragen nicht geladen");
    expect(root.querySelector("details.requests [role='alert']")?.textContent).toContain("boom");
  });
});

function customer(overrides: Partial<Customer>): Customer {
  return {
    id: "c1",
    name: "Katja Karsch",
    website: "https://www.katjakarsch.de/",
    industry: "kunst_kreativ",
    city: "Münster",
    order_value_eur: null,
    created_at: "2026-11-01T08:00:00Z",
    crm_customer_id: null,
    ...overrides,
  };
}

describe("Prüfen, customers", () => {
  it("lists the customers for the administrator only", async () => {
    listCustomers.mockResolvedValue([customer({})]);
    await mount(false);
    expect(listCustomers).not.toHaveBeenCalled();
    expect(root.querySelector(".customers")).toBeNull();
  });

  it("starts the audit of a chosen customer with the stored selection", async () => {
    listCustomers.mockResolvedValue([customer({}), customer({ id: "c2", name: "Atelier Nord", website: null, city: "" })]);
    startCustomerAudit.mockResolvedValue("audit-11");
    await mount(true);
    const buttons = [...root.querySelectorAll<HTMLButtonElement>(".customers button")];
    expect(buttons.map((button) => button.textContent)).toEqual(["Katja Karschkatjakarsch.de · Münster", "Atelier Nord"]);
    await act(() => {
      buttons[0]?.click();
    });
    expect(root.querySelector(".address-label")?.textContent).toBe("Katja Karsch");
    await submit(".choose");
    expect(startCustomerAudit).toHaveBeenCalledWith("c1", ["m1", "m2", "p5", "p6"]);
    expect(startFieldAudit).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?pruefung=audit-11");
  });

  it("shows the customers as a panel in the normal flow, outside the fixed layer", async () => {
    listCustomers.mockResolvedValue([customer({})]);
    await mount(true);
    const block = root.querySelector(".customers");
    expect(block?.classList.contains("panel")).toBe(true);
    expect(block?.closest(".requests-layer")).toBeNull();
  });

  it("names a failed customer load", async () => {
    listCustomers.mockRejectedValue(new Error("boom"));
    await mount(true);
    expect(root.querySelector(".customers [role='alert']")?.textContent).toContain("boom");
  });
});
