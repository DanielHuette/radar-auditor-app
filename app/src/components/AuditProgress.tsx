import { useAuditProgress } from "../hooks/auditProgress";
import type { AuditWithCustomer, CheckResult } from "../api/types";
import { CATALOGUE_AREAS } from "../lib/catalogue";
import { areaLines, type LineContext } from "../lib/checkLine";
import { orderedAreas } from "../lib/checkSelection";
import { controlRunning } from "../lib/checkVerdicts";
import { formatDateTime } from "../lib/format";
import { CONTROL_RUNNING_LABEL } from "../lib/labels";
import {
  analysisWaitText,
  EVENT_RESULT_LABELS,
  EVENT_RESULT_TONES,
  managerNote,
  PROGRESS_LABELS,
  PROGRESS_STEPS,
  type ManagerNote,
  type Progress,
  type RepairLine,
  type RouteLine,
} from "../lib/progress";
import type { ManagerEvent } from "../api/types";
import { CheckProgressView } from "./CheckProgressView";
import { CostEstimate } from "./CostEstimate";
import { ErrorNote, Loading } from "./ui";

function stepState(index: number, current: number): "past" | "current" | "next" {
  if (index < current) return "past";
  return index === current ? "current" : "next";
}

function Steps({ progress }: { progress: Progress }) {
  const current = PROGRESS_STEPS.indexOf(progress.step);
  return (
    <ol class="steps" aria-label="Fortschritt">
      {PROGRESS_STEPS.map((step, index) => {
        const state = stepState(index, current);
        return (
          <li key={step} data-state={state} aria-current={state === "current" ? "step" : undefined}>
            {PROGRESS_LABELS[step]}
          </li>
        );
      })}
    </ol>
  );
}

function StepOrProblem({ progress }: { progress: Progress }) {
  if (progress.step === "failed") {
    return (
      <p class="notice notice--bad" role="alert">
        <strong>{PROGRESS_LABELS.failed}:</strong> {progress.error ?? "Die Prüfung ist fehlgeschlagen."}
      </p>
    );
  }
  if (progress.step === "halted") {
    return (
      <p class="notice" role="status">
        <strong>{PROGRESS_LABELS.halted}:</strong> {progress.error ?? "Die Prüfung wartet auf einen Schritt von dir."}
      </p>
    );
  }
  return <Steps progress={progress} />;
}

function ProgressState({ progress }: { progress: Progress }) {
  return (
    <>
      <StepOrProblem progress={progress} />
      {progress.serpRetry && (
        <p class="notice" role="status">
          Google-Suche wird nachgeholt. Die Prüfung läuft weiter und wird neu bewertet, sobald die Suche durch ist.
        </p>
      )}
      {progress.analysisWait !== null && (
        <p class="notice" role="status">
          {analysisWaitText(progress.analysisWait, new Date())}
        </p>
      )}
    </>
  );
}

export function RouteList({ routes, checksScored }: { routes: readonly RouteLine[]; checksScored: number }) {
  if (routes.length === 0 && checksScored === 0) return null;
  return (
    <div class="stack stack--tight">
      <h3>Messungen</h3>
      <ul class="routes">
        {routes.map((route) => (
          <li key={route.key} class="route">
            <span class="route__label">{route.label}</span>
            <span class={`badge ${EVENT_RESULT_TONES[route.result]}`}>{EVENT_RESULT_LABELS[route.result]}</span>
            {route.reason !== null && <span class="route__reason">{route.reason}</span>}
          </li>
        ))}
      </ul>
      {checksScored > 0 && <p class="muted small">{checksScored === 1 ? "1 Prüfung bewertet" : `${String(checksScored)} Prüfungen bewertet`}</p>}
    </div>
  );
}

interface CheckProgressListProps {
  audit: AuditWithCustomer;
  checks: readonly CheckResult[];
  context: Omit<LineContext, "auditStatus">;
}

export function CheckProgressList({ audit, checks, context }: CheckProgressListProps) {
  const groups = areaLines(orderedAreas(CATALOGUE_AREAS), checks, audit.disabled_checks ?? [], { ...context, auditStatus: audit.status });
  return (
    <div class="stack stack--tight" aria-live="off">
      <h3>Prüfungen</h3>
      {groups.map((group) => (
        <section key={group.area} class="stack stack--tight" aria-label={group.name}>
          <p class="check__name">{group.name}</p>
          <ul class="routes routes--checks">
            {group.lines.map((line) => (
              <li key={line.checkId} class="route">
                <span class="route__label">{line.name}</span>
                {line.statusLabel !== null && <span class="badge">{line.statusLabel}</span>}
                {line.reason !== null && <span class="route__reason">{line.reason}</span>}
                {(line.progress !== null || line.boxes.length > 0) && (
                  <div class="route__reason">
                    <CheckProgressView line={line} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export function RepairList({ repairs }: { repairs: readonly RepairLine[] }) {
  if (repairs.length === 0) return null;
  return (
    <div class="stack stack--tight">
      <h3>Neustarts und Übernahmen durch den Audit-Agenten</h3>
      <ul class="routes">
        {repairs.map((repair) => (
          <li key={repair.key} class="route">
            <span class="route__label">
              {formatDateTime(repair.time)} {repair.label}
            </span>
            <span class={`badge ${EVENT_RESULT_TONES[repair.result]}`}>{EVENT_RESULT_LABELS[repair.result]}</span>
            {repair.reason !== null && <span class="route__reason">{repair.reason}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ManagerLine({ events }: { events: readonly ManagerEvent[] }) {
  const note: ManagerNote | null = managerNote(events);
  if (note === null) return null;
  return (
    <p class={note.tone === "bad" ? "notice notice--bad" : "notice"} role={note.tone === "bad" ? "alert" : "status"}>
      {note.text}
    </p>
  );
}

function ProgressActions({ auditId, done, onNew }: { auditId: string; done: boolean; onNew: () => void }) {
  return (
    <div class="cluster progress__actions">
      {done && (
        <a class="btn btn--primary btn--main btn--long" href={`/auswertung/${auditId}`}>
          Auswertung ansehen
        </a>
      )}
      <button type="button" class="btn btn--ghost" data-write onClick={onNew}>
        Neue Prüfung
      </button>
    </div>
  );
}

export function AuditProgress({ auditId, onNew }: { auditId: string; onNew: () => void }) {
  const { audit, progress, manager, routes, repairs, checksScored, estimates, checks, states, eventReasons, error } = useAuditProgress(auditId);

  if (audit === undefined) return error === null ? <Loading label="Prüfung wird geladen …" /> : <ErrorNote message={error} />;
  if (audit === null || progress === null) return <ErrorNote message="Diese Prüfung gibt es nicht mehr." />;

  return (
    <section class="panel stack progress" aria-labelledby="progress-title" aria-live="polite">
      <div>
        <h2 id="progress-title">{audit.customers?.name ?? "Prüfung"}</h2>
        <p class="muted small">gestartet {formatDateTime(audit.requested_at)}</p>
      </div>
      <ProgressState progress={progress} />
      {controlRunning(checks) && (
        <p class="notice" role="status">
          {CONTROL_RUNNING_LABEL}: Es liegen noch nicht alle Urteile zu den Prüfungen vor.
        </p>
      )}
      <CostEstimate estimates={estimates} status={audit.estimate_status} note={audit.estimate_note} customerName={audit.customers?.name ?? ""} requestedAt={audit.requested_at} />
      <RouteList routes={routes} checksScored={checksScored} />
      <CheckProgressList audit={audit} checks={checks} context={{ states, eventReasons }} />
      <RepairList repairs={repairs} />
      <ManagerLine events={manager} />
      {error !== null && <p class="field__error">{error}</p>}
      <ProgressActions auditId={audit.id} done={progress.step === "done"} onNew={onNew} />
    </section>
  );
}
