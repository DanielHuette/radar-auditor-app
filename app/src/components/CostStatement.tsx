import type { CostStatement as Statement, PaidLine, UsageLine } from "../lib/costs";
import { formatEuroPrecise, formatNumber } from "../lib/format";

function PaidList({ paid, total }: { paid: readonly PaidLine[]; total: number }) {
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
          <strong>Gesamtsumme</strong>
        </span>
        <span class="costs__sum">{formatEuroPrecise(total)}</span>
      </li>
    </ul>
  );
}

function UsageList({ usage }: { usage: readonly UsageLine[] }) {
  return (
    <ul class="costs">
      {usage.map((line) => (
        <li key={`${line.source}/${line.service}`} class="costs__row">
          <span class="costs__name">
            <strong>{line.source}</strong>
            <span class="muted small">{line.service}</span>
          </span>
          <span class="costs__calc muted small">
            {formatNumber(line.calls)} {line.calls === 1 ? "Aufruf" : "Aufrufe"}
          </span>
          <span class="costs__sum small">
            {formatNumber(line.tokensIn)} Token gelesen, {formatNumber(line.tokensOut)} geschrieben
          </span>
        </li>
      ))}
    </ul>
  );
}

export function CostStatement({ statement }: { statement: Statement }) {
  const { paid, total, usage } = statement;
  return (
    <div class="stack">
      {paid.length === 0 ? <p class="muted">Für diese Prüfung wurde keine bezahlte Abfrage gebucht.</p> : <PaidList paid={paid} total={total} />}
      {usage.length > 0 && (
        <>
          <h3>Verbrauch ohne Eurobetrag</h3>
          <UsageList usage={usage} />
        </>
      )}
    </div>
  );
}
