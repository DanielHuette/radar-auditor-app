// @vitest-environment happy-dom
import { render } from "preact";
import { afterEach, describe, expect, it } from "vitest";
import { AiNotice } from "./AiNotice";

let root: HTMLDivElement;

afterEach(() => {
  render(null, root);
});

describe("AiNotice", () => {
  it("states in one line that the finding and recommendation texts come from artificial intelligence", () => {
    root = document.createElement("div");
    render(<AiNotice />, root);
    const line = root.querySelector("p.muted.small");
    expect(line?.textContent).toBe("Die Texte zu Befunden, Auswertung und Empfehlungen wurden mit künstlicher Intelligenz erstellt.");
    expect(root.children).toHaveLength(1);
  });
});
