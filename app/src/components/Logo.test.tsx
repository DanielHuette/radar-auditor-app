// @vitest-environment happy-dom
import { render } from "preact";
import { describe, expect, it } from "vitest";
import { Logo } from "./Logo";

function mount(label?: string) {
  const root = document.createElement("div");
  render(<Logo label={label} />, root);
  return root;
}

describe("Logo", () => {
  it("shows the app label under the wordmark", () => {
    const root = mount("RadarAuditor");
    expect(root.querySelector(".logo__label")?.textContent).toBe("RadarAuditor");
    expect(root.querySelector(".logo__words .wordmark")).not.toBeNull();
  });

  it("shows no label without one", () => {
    expect(mount().querySelector(".logo__label")).toBeNull();
  });
});
