import { render } from "preact";
import { missingConfig } from "./api/config";
import { Logo } from "./components/Logo";
import { captureInstallPrompt } from "./hooks/install";
import { installPointerLight } from "./hooks/motion";
import "./theme/tokens.css";
import "./theme/app.css";

const root = document.getElementById("app");
if (root === null) throw new Error("Element #app is missing in index.html");

captureInstallPrompt();
installPointerLight();

function ConfigMissing({ names }: { names: readonly string[] }) {
  return (
    <main class="signin">
      <div class="signin__card panel stack">
        <Logo size={48} />
        <h1>App nicht eingerichtet</h1>
        <p class="muted">
          Beim Bauen fehlte {names.join(", ")}. Trag die Werte in Cloudflare Pages unter Einstellungen,
          Umgebungsvariablen ein und starte den Build neu.
        </p>
      </div>
    </main>
  );
}

const missing = missingConfig();
if (missing.length > 0) {
  render(<ConfigMissing names={missing} />, root);
} else {
  void import("./App").then(({ App }) => {
    render(<App />, root);
  });
}
