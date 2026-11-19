import { useLocation } from "preact-iso";
import { useState } from "preact/hooks";
import {
  auditChecks,
  auditResults,
  auditTexts,
  getAudit,
  getCustomer,
  listAuditCosts,
  listAuditEstimates,
  listAuditEvents,
  listAudits,
  listKeywords,
  listRankings,
  reportDownloadUrl,
  reportUrl,
} from "../api/queries";
import type { Audit, AuditWithCustomer, CheckResult, Customer, Finding } from "../api/types";
import { AiNotice } from "../components/AiNotice";
import { AreaTrafficLights } from "../components/AreaTrafficLights";
import { ManagerLine, RepairList, RouteList } from "../components/AuditProgress";
import { CheckRow } from "../components/CheckRow";
import { CompetitorBars } from "../components/CompetitorBars";
import { CostEstimate } from "../components/CostEstimate";
import { CostStatement } from "../components/CostStatement";
import { CrmButton } from "../components/CrmButton";
import { FindingCard } from "../components/FindingCard";
import { GridMap } from "../components/GridMap";
import { IdentityBar } from "../components/IdentityBar";
import { JobStatusBadge } from "../components/JobStatusBadge";
import { KeywordScopes } from "../components/KeywordScopes";
import { Levers } from "../components/Levers";
import { downloadFile, openInNewTab } from "../components/openUrl";
import { RadarChart } from "../components/RadarChart";
import { ReviewReplies } from "../components/ReviewReplies";
import { ScoreRing } from "../components/ScoreRing";
import { ServerStatusChip } from "../components/ServerStatusChip";
import { TalkBox } from "../components/TalkBox";
import { TextSuggestions } from "../components/TextSuggestions";
import { TrendLine } from "../components/TrendLine";
import { UnmeasuredChecks } from "../components/UnmeasuredChecks";
import { ChipGroup, Empty, ErrorNote, Loaded, Select, Toggle } from "../components/ui";
import { useAction, useAsync } from "../hooks/useAsync";
import { useLive } from "../hooks/useLive";
import {
  buildRows,
  checkEventReasons,
  formatPotential,
  groupRows,
  orphanFindings,
  potentialTotals,
  previousAudit,
  radarAxes,
  SORT_LABELS,
  topLevers,
  type CheckRow as Row,
  type PotentialTotals,
  type SortMode,
  unmeasuredChecks,
} from "../lib/evaluation";
import type { LineContext } from "../lib/checkLine";
import { stepStates } from "../lib/checkProgress";
import { controlRunning } from "../lib/checkVerdicts";
import { reportFileName } from "../lib/reportFile";
import { costStatement } from "../lib/costs";
import { formatDate, formatDateTime, formatScore } from "../lib/format";
import { AUDIT_KIND_LABELS, AUDIT_STATUS_LABELS, CONTROL_RUNNING_LABEL } from "../lib/labels";
import { checkEventCount, isFinished, managerEvents, repairLines, routeLines, stepOfAuditStatus } from "../lib/progress";
import { coverageOf, groupByArea, sortFindings } from "../lib/scoring";

const SWITCHER_AUDITS = 10;
const SORT_MODES: readonly SortMode[] = ["potential", "area", "urgency"];

async function loadAudit(id: string) {
  const audit = await getAudit(id);
  if (audit === null) return null;
  const [results, rankings, keywords, history, customer, costs, events, estimates] = await Promise.all([
    auditResults(id),
    listRankings(id),
    listKeywords(audit.customer_id),
    listAudits(audit.customer_id),
    getCustomer(audit.customer_id),
    listAuditCosts(id),
    listAuditEvents(id),
    listAuditEstimates(id),
  ]);
  const previous = previousAudit(history, audit);
  const previousChecks = previous === null ? null : await auditChecks(previous.id);
  return { audit, customer, ...results, rankings, keywords, history, previous, previousChecks, costs, estimates, events };
}

const LIVE_TABLES = (id: string) => [
  { table: "audits", filter: `id=eq.${id}` },
  { table: "check_results", filter: `audit_id=eq.${id}` },
  { table: "audit_events", filter: `audit_id=eq.${id}` },
  { table: "findings", filter: `audit_id=eq.${id}` },
];

function statusHint(audit: AuditWithCustomer): string | null {
  switch (audit.status) {
    case "requested":
      return "Wartet darauf, dass der Server den Auftrag abholt.";
    case "collecting":
    case "collected":
    case "scored":
    case "controlling":
      return "Der Server arbeitet. Ergebnisse erscheinen hier, sobald sie da sind.";
    case "awaiting_analysis":
      return "Die Befunde werden gerade geschrieben.";
    case "analysed":
      return "Befunde sind da, der Bericht wird gerade erstellt.";
    case "failed":
      return audit.error === null ? "Die Prüfung ist fehlgeschlagen." : `Fehlgeschlagen: ${audit.error}`;
    case "halted":
      return audit.error === null ? "Die Prüfung ist angehalten und wartet auf dich." : `Angehalten: ${audit.error}`;
    case "reported":
      return null;
  }
}

function auditOption(audit: Audit): string {
  const result = audit.total_score === null ? AUDIT_STATUS_LABELS[audit.status] : `${formatScore(audit.total_score)} Punkte`;
  return `${formatDate(audit.requested_at)} · ${result}`;
}

function AuditSwitcher({ current, history }: { current: Audit; history: readonly Audit[] }) {
  const { route } = useLocation();
  const latest = history.slice(0, SWITCHER_AUDITS);
  const audits = latest.some((audit) => audit.id === current.id) ? latest : [current, ...latest];
  if (audits.length < 2) return null;
  return (
    <Select
      label="Prüfung wechseln"
      value={current.id}
      options={audits.map((audit) => ({ value: audit.id, label: auditOption(audit) }))}
      onChange={(id) => {
        route(`/auswertung/${id}`);
      }}
    />
  );
}

function PotentialSummary({ totals }: { totals: PotentialTotals }) {
  return (
    <section class="panel potential" aria-labelledby="potential-title">
      <h2 id="potential-title">Offenes Potenzial</h2>
      {!totals.known ? (
        <p class="muted">Für diese Prüfung ist noch kein Potenzial berechnet.</p>
      ) : totals.openChecks === 0 ? (
        <p>Alle messbaren Prüfungen sind bestanden.</p>
      ) : (
        <>
          <p class="potential__points">{formatPotential(totals.points)}</p>
          <p class="muted small">
            aus {String(totals.openChecks)} {totals.openChecks === 1 ? "offenen Prüfung" : "offenen Prüfungen"}
          </p>
        </>
      )}
    </section>
  );
}

function CheckGroups({ groups, context }: { groups: ReturnType<typeof groupRows>; context: LineContext }) {
  return (
    <>
      {groups.map((group) => (
        <section key={group.area} class="panel check-group" aria-label={group.name}>
          <div class="check-group__head">
            <h3>{group.name}</h3>
            {group.potential > 0 && <span class="check__points">{formatPotential(group.potential)}</span>}
          </div>
          <ul class="checks">
            {group.rows.map((row) => (
              <CheckRow key={row.check.id} row={row} context={context} />
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}

function CheckList({ rows, context }: { rows: readonly Row[]; context: LineContext }) {
  const [mode, setMode] = useState<SortMode>("potential");
  const [onlyOpen, setOnlyOpen] = useState(false);
  const groups = groupRows(rows, mode, onlyOpen);
  return (
    <section class="stack" aria-labelledby="checks-title">
      <h2 id="checks-title">Alle Prüfungen</h2>
      <div class="stack stack--tight">
        <ChipGroup
          label="Sortieren nach"
          values={SORT_MODES}
          selected={mode}
          labelOf={(option) => SORT_LABELS[option]}
          onSelect={setMode}
        />
        <Toggle label="Nur offenes Potenzial" checked={onlyOpen} onChange={setOnlyOpen} />
      </div>
      {groups.length === 0 ? (
        <div class="panel">
          <Empty>{rows.length === 0 ? "Noch keine Prüfergebnisse." : "Kein offenes Potenzial."}</Empty>
        </div>
      ) : (
        <CheckGroups groups={groups} context={context} />
      )}
    </section>
  );
}

function ReportButton({ audit }: { audit: AuditWithCustomer }) {
  const opening = useAction();
  if (audit.report_path === null) return null;
  const fileName = reportFileName(audit.customers?.name ?? "", audit.requested_at);
  const run = (task: () => Promise<boolean>) => {
    void opening.run(async () => {
      await task();
    });
  };
  return (
    <div class="stack stack--tight">
      <button type="button" class="btn btn--primary btn--long" disabled={opening.busy} onClick={() => { run(() => downloadFile(() => reportDownloadUrl(audit, fileName))); }}>
        Bericht herunterladen (PDF)
      </button>
      <button type="button" class="btn btn--long" disabled={opening.busy} onClick={() => { run(() => openInNewTab(() => reportUrl(audit))); }}>
        Bericht öffnen
      </button>
      {opening.error !== null && <p class="field__error">{opening.error}</p>}
    </div>
  );
}

function ScoreSection({ checks, previous, previousChecks, audit }: {
  checks: readonly CheckResult[];
  previous: Audit | null;
  previousChecks: readonly CheckResult[] | null;
  audit: Audit;
}) {
  const axes = radarAxes(checks, previous === null ? null : previousChecks);
  const total = coverageOf(checks);
  return (
    <div class="score-layout">
      <section class="panel stack" aria-labelledby="total">
        <h2 id="total">Gesamtpunkte</h2>
        <ScoreRing score={audit.total_score} />
        <p class="muted small">aus {String(total.counted)} von {String(total.ratable)} wertbaren Prüfungen</p>
        {checks.length > 0 && (
          <RadarChart
            axes={axes}
            currentLabel={`Diese Prüfung, ${formatDate(audit.requested_at)}`}
            previousLabel={previous === null ? null : `Vorherige, ${formatDate(previous.requested_at)}`}
          />
        )}
      </section>
      <section class="panel" aria-labelledby="areas">
        <h2 id="areas">Gesamtwert je Anbieter</h2>
        {checks.length === 0 ? <Empty>Noch keine Prüfergebnisse.</Empty> : <AreaTrafficLights areas={groupByArea(checks)} />}
      </section>
    </div>
  );
}

type LoadedAudit = NonNullable<Awaited<ReturnType<typeof loadAudit>>>;

function TextsError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section class="panel" aria-label="Texte zur Prüfung">
      <ErrorNote message={message} onRetry={onRetry} />
    </section>
  );
}

function EvaluationHead({ audit, customer, customerName, checks }: { audit: AuditWithCustomer; customer: Customer | null; customerName: string; checks: readonly CheckResult[] }) {
  return (
    <header class="page-head">
      <div>
        <a class="back" href="/auswertung">
          ← Alle Prüfungen
        </a>
        <h1>{customerName || AUDIT_KIND_LABELS[audit.kind]}</h1>
        <p class="muted">
          {AUDIT_KIND_LABELS[audit.kind]} · {formatDateTime(audit.requested_at)}
        </p>
      </div>
      <div class="cluster">
        <JobStatusBadge status={audit.status} controlRunning={controlRunning(checks)} />
        {customer !== null && <CrmButton crmCustomerId={customer.crm_customer_id} />}
      </div>
    </header>
  );
}

function OrphanFindings({ checks, findings }: { checks: readonly CheckResult[]; findings: readonly Finding[] }) {
  const orphans = sortFindings(orphanFindings(checks, findings));
  if (orphans.length === 0) return null;
  return (
    <section class="stack" aria-labelledby="orphans">
      <h2 id="orphans">Weitere Befunde</h2>
      {orphans.map((finding) => (
        <FindingCard key={finding.id} finding={finding} />
      ))}
    </section>
  );
}

function RankingSections({ loaded, customerName }: { loaded: LoadedAudit; customerName: string }) {
  const { rankings, keywords, history } = loaded;
  return (
    <>
      <div class="grid-2">
        <section class="panel" aria-labelledby="competitors">
          <h2 id="competitors">Platzierungen im Vergleich</h2>
          <CompetitorBars rankings={rankings} keywords={keywords} customerName={customerName} />
        </section>
        <section class="panel" aria-labelledby="grid">
          <h2 id="grid">Kartenraster</h2>
          <GridMap rankings={rankings} keywords={keywords} customerName={customerName} />
        </section>
      </div>

      <section class="panel" aria-labelledby="trend">
        <h2 id="trend">Verlauf</h2>
        <TrendLine audits={history} />
      </section>
    </>
  );
}

function MarketSections({ loaded, customerName }: { loaded: LoadedAudit; customerName: string }) {
  return (
    <>
      <KeywordScopes keywords={loaded.keywords} rankings={loaded.rankings} customerName={customerName} />
      <RankingSections loaded={loaded} customerName={customerName} />
    </>
  );
}

function CostSection({ loaded }: { loaded: LoadedAudit }) {
  const { audit, costs, estimates } = loaded;
  return (
    <section class="panel stack" aria-labelledby="costs">
      <h2 id="costs">Was diese Prüfung gekostet hat</h2>
      <CostEstimate estimates={estimates} status={audit.estimate_status} note={audit.estimate_note} customerName={audit.customers?.name ?? ""} requestedAt={audit.requested_at} />
      <div class="stack stack--tight">
        <h3>Kostenaufstellung</h3>
        <CostStatement statement={costStatement(costs)} />
      </div>
    </section>
  );
}

function MeasurementSection({ loaded }: { loaded: LoadedAudit }) {
  const { audit, events } = loaded;
  const routes = routeLines(events);
  const manager = managerEvents(events);
  const running = !isFinished(stepOfAuditStatus(audit.status)) && audit.status !== "halted";
  if (routes.length === 0 && manager.length === 0 && !running) return null;
  return (
    <section class="panel stack" aria-labelledby="measurements">
      <h2 id="measurements">Verlauf der Messung</h2>
      {running && <ServerStatusChip />}
      <ManagerLine events={manager} />
      <RouteList routes={routes} checksScored={checkEventCount(events)} />
      <RepairList repairs={repairLines(events)} />
    </section>
  );
}

function CheckSections({ loaded }: { loaded: LoadedAudit }) {
  const { audit, checks, findings, events } = loaded;
  const eventReasons = checkEventReasons(events);
  const context: LineContext = { auditStatus: audit.status, states: stepStates(events), eventReasons };
  return (
    <>
      <CheckList rows={buildRows(checks, findings, eventReasons)} context={context} />
      <UnmeasuredChecks checks={unmeasuredChecks(checks, audit.disabled_checks ?? [])} />
      <OrphanFindings checks={checks} findings={findings} />
    </>
  );
}

function StatusNotices({ audit, checks }: { audit: AuditWithCustomer; checks: readonly CheckResult[] }) {
  const hint = statusHint(audit);
  return (
    <>
      {hint !== null && <p class={audit.status === "failed" ? "notice notice--bad" : "notice"}>{hint}</p>}
      {controlRunning(checks) && (
        <p class="notice" role="status">
          {CONTROL_RUNNING_LABEL}: Es liegen noch nicht alle Urteile zu den Prüfungen vor. Geprüfte Ergebnisse stehen schon unten.
        </p>
      )}
    </>
  );
}

function EvaluationView({ loaded }: { loaded: LoadedAudit }) {
  const { audit, customer, checks, findings, history, previous, previousChecks } = loaded;
  const texts = useAsync(() => auditTexts(audit.id), [audit.id]);
  const customerName = audit.customers?.name ?? "";
  const talk = texts.data?.talk ?? null;
  return (
    <div class="stack">
      <EvaluationHead audit={audit} customer={customer} customerName={customerName} checks={checks} />
      {customer !== null && <IdentityBar customer={customer} audit={audit} />}
      {texts.error !== null && <TextsError message={texts.error} onRetry={texts.reload} />}
      {(findings.length > 0 || talk !== null) && <AiNotice />}
      {talk !== null && <TalkBox text={talk} />}
      <Levers levers={topLevers(checks, findings)} />
      <div class="evaluation-tools">
        <AuditSwitcher current={audit} history={history} />
        <ReportButton audit={audit} />
      </div>
      <StatusNotices audit={audit} checks={checks} />
      {customer !== null && <ReviewReplies auditId={audit.id} auditStatus={audit.status} customerId={customer.id} />}

      <MeasurementSection loaded={loaded} />
      <PotentialSummary totals={potentialTotals(checks)} />
      <ScoreSection checks={checks} previous={previous} previousChecks={previousChecks} audit={audit} />
      <CheckSections loaded={loaded} />
      {texts.data !== undefined && texts.data !== null && <TextSuggestions texts={texts.data} />}
      <MarketSections loaded={loaded} customerName={customerName} />
      <CostSection loaded={loaded} />
    </div>
  );
}

export function AuditEvaluation({ id }: { id: string }) {
  const live = useLive(id, () => loadAudit(id), LIVE_TABLES(id), (data) => data !== null && controlRunning(data.checks));
  const state = { ...live, loading: live.data === undefined && live.error === null };
  return (
    <Loaded state={state} label="Prüfung wird geladen …">
      {(loaded) => (loaded === null ? <ErrorNote message="Diese Prüfung gibt es nicht mehr." /> : <EvaluationView loaded={loaded} />)}
    </Loaded>
  );
}
