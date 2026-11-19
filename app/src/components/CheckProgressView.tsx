import type { BoxState, VerdictBox } from "../lib/checkVerdicts";
import { barPercent, BAR_LABELS, controlWaitText, type CheckProgress } from "../lib/checkProgress";
import type { CheckLine } from "../lib/checkLine";

const BOX_SYMBOLS: Record<BoxState, string> = { open: "☐", ok: "☑", bad: "☒" };
const BOX_WORDS: Record<BoxState, string> = { open: "noch offen", ok: "Haken", bad: "X" };
const BOX_TONES: Record<BoxState, string> = { open: "", ok: "tone-ok", bad: "tone-bad" };

export const NOT_COUNTED_TEXT = "Zählt nicht in den Gesamtwert.";
export const EVIDENCE_LABEL = "Belegstelle";

function barTone(progress: CheckProgress): string {
  if (progress.wait !== null) return "tone-warn";
  if (progress.stage === "done") return "tone-ok";
  return progress.stage === "waiting" ? "" : "tone-busy";
}

export function ProgressBar({ progress }: { progress: CheckProgress }) {
  const percent = barPercent(progress.stage);
  const label = progress.wait === null ? BAR_LABELS[progress.stage] : controlWaitText(progress.wait);
  return (
    <div class="area__name">
      <span
        class={`area__bar ${barTone(progress)}`}
        role="progressbar"
        aria-label="Fortschritt dieser Prüfung"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={label}
      >
        <span class="area__fill" style={{ width: `${String(percent)}%` }} />
      </span>
      <span class="muted small">{label}</span>
    </div>
  );
}

function BoxText({ box }: { box: VerdictBox }) {
  return (
    <>
      <span aria-hidden="true">{BOX_SYMBOLS[box.state]}</span> {box.label}
      <span class="visually-hidden">: {BOX_WORDS[box.state]}</span>
    </>
  );
}

function detailOf(box: VerdictBox): string {
  const reason = box.reason ?? "";
  return box.evidence === null ? reason : `${reason} ${EVIDENCE_LABEL}: ${box.evidence}`;
}

export function VerdictBoxView({ box }: { box: VerdictBox }) {
  if (box.state !== "bad") {
    return (
      <span class={`badge ${BOX_TONES[box.state]}`}>
        <BoxText box={box} />
      </span>
    );
  }
  return (
    <details>
      <summary class="badge tone-bad" title={detailOf(box)}>
        <BoxText box={box} />
      </summary>
      <div class="stack stack--tight">
        <p>{box.reason}</p>
        {box.evidence !== null && (
          <p class="muted small">
            {EVIDENCE_LABEL}: {box.evidence}
          </p>
        )}
      </div>
    </details>
  );
}

export function VerdictBoxes({ boxes }: { boxes: readonly VerdictBox[] }) {
  if (boxes.length === 0) return null;
  return (
    <div class="cluster" role="group" aria-label="Urteile der Kontrolle">
      {boxes.map((box) => (
        <VerdictBoxView key={box.key} box={box} />
      ))}
    </div>
  );
}

export function CheckProgressView({ line }: { line: CheckLine }) {
  if (line.progress === null && line.boxes.length === 0) return null;
  return (
    <div class="stack stack--tight">
      {line.progress !== null && <ProgressBar progress={line.progress} />}
      <VerdictBoxes boxes={line.boxes} />
      {line.excluded && <p class="muted small">{NOT_COUNTED_TEXT}</p>}
    </div>
  );
}
