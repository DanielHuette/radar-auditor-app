import type { AuditEstimate, EstimateStatus } from "../api/types";
import type { EstimateDocument } from "../lib/estimatePdf";
import { estimateStatement, type PaidLine, type PlannedUse } from "../lib/costs";
import { formatEuroPrecise, formatNumber } from "../lib/format";
import { estimateFileName } from "../lib/reportFile";
import { useAction } from "../hooks/useAsync";
import { saveFile } from "./openUrl";

const STATUS_LABELS: Record<EstimateStatus, { text: string; tone: string }> = {
  covered: { text: "Guthaben und Limits decken den Voranschlag", tone: "notice" },
  insufficient: { text: "Guthaben oder Limits reichen nicht, die Prüfung startet nicht", tone: "notice notice--bad" },
  unchecked: { text: "Guthaben konnte nicht geprüft werden", tone: "notice" },
};

interface CostEstimateProps {
  estimates: readonly AuditEstimate[];
  status: EstimateStatus | null | undefined;
  note: string | null | undefined;
  customerName: string;
  requestedAt: string;
}

function PlannedList({ planned }: { planned: readonly PlannedUse[] }) {
  return (
    <ul class="costs">
      {planned.map((line) => (
        <li key={`${line.source}/${line.service}`} class="costs__row">
          <span class="costs__name">
            <strong>{line.source}</strong>
            <span class="muted small">{line.service}</span>
          </span>
          <span class="costs__sum small">{formatNumber(line.calls)} {line.calls === 1 ? "Aufruf" : "Aufrufe"}, ohne Eurobetrag</span>
        </li>
      ))}
    </ul>
  );
}

function PaidEstimate({ paid, total }: { paid: readonly PaidLine[]; total: number }) {
  return (
    <ul class="costs">
      {paid.map((line) => (
        <li key={`${line.source}/${line.service}`} class="costs__row">
          <span class="costs__name">
            <strong>{line.source}</strong>
            <span class="muted small">{line.service}</span>
          </span>
          <span class="costs__calc muted small">
            {formatNumber(line.calls)} {line.calls === 1 ? "Abruf" : "Abrufe"} à {formatEuroPrecise(line.unitPrice)}
          </span>
          <span class="costs__sum">{formatEuroPrecise(line.total)}</span>
        </li>
      ))}
      <li class="costs__row costs__row--total">
        <span class="costs__name">
          <strong>Voranschlag gesamt</strong>
        </span>
        <span class="costs__sum">{formatEuroPrecise(total)}</span>
      </li>
    </ul>
  );
}

function EstimateDownload({ document }: { document: EstimateDocument }) {
  const saving = useAction();
  const save = () => {
    void saving.run(async () => {
      const { estimatePdf } = await import("../lib/estimatePdf");
      saveFile(estimateFileName(document.customerName, document.requestedAt), await estimatePdf(document), "application/pdf");
    });
  };
  return (
    <>
      <button type="button" class="btn btn--long" disabled={saving.busy} onClick={save}>
        Kostenvoranschlag herunterladen (PDF)
      </button>
      {saving.error !== null && <p class="field__error">{saving.error}</p>}
    </>
  );
}

export function CostEstimate({ estimates, status, note, customerName, requestedAt }: CostEstimateProps) {
  const statement = estimateStatement(estimates);
  const { paid, total, planned } = statement;
  if (estimates.length === 0 && (status === null || status === undefined)) return null;
  const verdict = status === null || status === undefined ? null : STATUS_LABELS[status];
  const detail = note?.trim() ?? "";
  const verdictText = verdict === null ? null : detail === "" ? verdict.text : `${verdict.text}: ${detail}`;
  return (
    <div class="stack stack--tight">
      <h3>Kostenvoranschlag</h3>
      {verdict !== null && (
        <p class={verdict.tone} role={status === "insufficient" ? "alert" : "status"}>
          {verdictText}
        </p>
      )}
      {paid.length > 0 && <PaidEstimate paid={paid} total={total} />}
      {planned.length > 0 && <PlannedList planned={planned} />}
      {estimates.length > 0 && <EstimateDownload document={{ customerName, requestedAt, verdict: verdictText, statement }} />}
    </div>
  );
}
