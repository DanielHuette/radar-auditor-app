// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { beforeAll, describe, expect, it } from "vitest";
import { Lockup } from "../components/Logo";
import { installPointerLight } from "./motion";

const nextFrame = () =>
  new Promise<void>((resolve) => {
    requestAnimationFrame(() => {
      resolve();
    });
  });

function movePointer(target: Element, pointerType: string): void {
  target.dispatchEvent(new PointerEvent("pointermove", { pointerType, clientX: 30, clientY: 40, bubbles: true }));
}

describe("installPointerLight", () => {
  beforeAll(() => {
    installPointerLight();
  });

  it("hands a panel the pointer position for its gold light", async () => {
    const panel = document.createElement("section");
    panel.className = "panel";
    document.body.append(panel);
    movePointer(panel, "mouse");
    await nextFrame();
    expect(panel.style.getPropertyValue("--mx")).toBe("30px");
    expect(panel.style.getPropertyValue("--my")).toBe("40px");
  });

  it("leaves a panel alone for touch input", async () => {
    const panel = document.createElement("section");
    panel.className = "panel";
    document.body.append(panel);
    movePointer(panel, "touch");
    await nextFrame();
    expect(panel.style.getPropertyValue("--mx")).toBe("");
  });

  it("leaves elements outside panels alone", async () => {
    const plain = document.createElement("div");
    document.body.append(plain);
    movePointer(plain, "mouse");
    await nextFrame();
    expect(plain.style.getPropertyValue("--mx")).toBe("");
  });
});

describe("Lockup", () => {
  it("tilts the sign-in logo on a spring", async () => {
    const root = document.createElement("div");
    await act(() => {
      render(<Lockup />, root);
    });
    await nextFrame();
    await nextFrame();
    const lockup = root.querySelector<HTMLElement>(".lockup");
    expect(lockup?.style.getPropertyValue("--tilt-x")).toMatch(/deg$/);
    render(null, root);
  });
});
