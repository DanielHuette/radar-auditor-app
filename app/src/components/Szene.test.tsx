// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Szene } from "./Szene";

afterEach(() => {
  vi.restoreAllMocks();
});

async function mount(leise: boolean): Promise<HTMLDivElement> {
  const root = document.createElement("div");
  await act(() => {
    render(<Szene leise={leise} />, root);
  });
  return root;
}

describe("Szene", () => {
  it("draws a hidden background canvas even where WebGL 2 is missing", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const root = await mount(false);
    const canvas = root.querySelector("canvas");
    expect(canvas?.className).toBe("buehne");
    expect(canvas?.getAttribute("aria-hidden")).toBe("true");
  });

  it("starts no animation when the shader does not compile", async () => {
    const gl = { createShader: vi.fn(() => null) };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(gl as unknown as WebGL2RenderingContext);
    const frames = vi.spyOn(window, "requestAnimationFrame");
    await mount(true);
    expect(gl.createShader).toHaveBeenCalled();
    expect(frames).not.toHaveBeenCalled();
  });
});
