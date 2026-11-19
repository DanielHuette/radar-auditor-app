import type { CheckOutcome } from "../api/types";
import type { CheckRow as Row } from "../lib/evaluation";
import { checkAnchor, formatPotential, isOpen, potentialOf } from "../lib/evaluation";
import { catalogueCheck, isInfoCheck } from "../lib/catalogue";
import { comparisonText } from "../lib/comparison";
import { checkLine, type LineContext } from "../lib/checkLine";
import { INFO_LABEL, OUTCOME_LABELS, OUTCOME_TONES, URGENCY } from "../lib/labels";
import { formatMeasured, starsLabel, unansweredReviews, type ReviewExample } from "../lib/measured";
import { paidRouteText } from "../lib/routes";
import { CheckProgressView } from "./CheckProgressView";
import { FindingBody } from "./FindingCard";
import { SourceLink } from "./SourceLink";

function Stars({ stars }: { stars: number }) {
  return (
    <span class="review-card__stars" role="img" aria-label={starsLabel(stars)}>
      {"★".repeat(Math.max(0, Math.round(stars)))}
    </span>
  );
}

function UnansweredReviews({ reviews }: { reviews: readonly ReviewExample[] }) {
  return (
    <div class="review-cards">
      <p class="review-cards__title">Ohne Antwort, zum Beispiel</p>
      <ul class="review-cards__list">
        {reviews.map((review, index) => (
          <li key={index} class="review-card">
            {(review.stars !== null || review.when !== null) && (
              <p class="review-card__meta">
                {review.stars !== null && <Stars stars={review.stars} />}
                {review.when !== null && <span>{review.when}</span>}
              </p>
            )}
            {review.text === "" ? <p class="review-card__text muted">ohne Text</p> : <p class="review-card__text">„{review.text}“</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

const RATED_OUTCOMES: ReadonlySet<CheckOutcome> = new Set(["passed", "partial", "failed"]);

function outcomeLabel(row: Row): { text: string; tone: string } {
  const { check } = row;
  if (isInfoCheck(check.check_id) && RATED_OUTCOMES.has(check.outcome)) return { text: INFO_LABEL, tone: "" };
  return { text: OUTCOME_LABELS[check.outcome], tone: OUTCOME_TONES[check.outcome] };
}

function CheckSummary({ row }: { row: Row }) {
  const { check, finding } = row;
  const outcome = outcomeLabel(row);
  const open = isOpen(check);
  const potential = potentialOf(check);
  const route = paidRouteText(check.measured);
  const comparison = isInfoCheck(check.check_id) ? comparisonText({ checkId: check.check_id, measured: check.measured }) : null;
  return (
    <summary class="check__summary">
      <span class="check__name">{row.name}</span>
      <span class="check__meta">
        <span class={`badge ${outcome.tone}`}>{outcome.text}</span>
        {finding !== null && <span class={`badge ${URGENCY[finding.severity].tone}`}>{URGENCY[finding.severity].label}</span>}
        {open && potential > 0 && <span class="check__points">{formatPotential(potential)}</span>}
      </span>
      {route !== null && <span class="check__route">{route}</span>}
      {comparison !== null && <span class="check__route">Messwert und Vergleich: {comparison}</span>}
      {row.reason !== null && (
        <span class="check__reason">
          {check.outcome === "not_applicable" ? "Nicht zutreffend" : "Nicht gemessen"}: {row.reason}
        </span>
      )}
    </summary>
  );
}

function Measured({ row }: { row: Row }) {
  const explanation = row.finding?.explanation.trim() ?? "";
  const lines = formatMeasured(row.check.measured);
  const reviews = unansweredReviews(row.check.measured);
  if (explanation === "" && lines.length === 0 && reviews.length === 0) return null;
  return (
    <div class="check__measured">
      <p class="check__label">Gemessen</p>
      {explanation !== "" && <p>{row.finding?.explanation}</p>}
      {lines.length > 0 && (
        <dl class="measured">
          {lines.map((line) => (
            <div key={line.key}>
              <dt>{line.label}</dt>
              <dd>{line.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {reviews.length > 0 && <UnansweredReviews reviews={reviews} />}
    </div>
  );
}

function CustomerCost({ impact }: { impact: string }) {
  return (
    <div class="finding">
      <dl>
        <div>
          <dt>Was es den Kunden kostet</dt>
          <dd>{impact}</dd>
        </div>
      </dl>
    </div>
  );
}

export function CheckRow({ row, context }: { row: Row; context: LineContext }) {
  const { check, finding } = row;
  const entry = catalogueCheck(check.check_id);
  const line = entry === undefined ? null : checkLine(entry, check, context);
  return (
    <li class="check" id={checkAnchor(check.check_id)}>
      <details>
        <CheckSummary row={row} />
        <div class="check__body">
          {entry !== undefined && <p class="check__why">{entry.why}</p>}
          {entry !== undefined && entry.source !== "" && (
            <p class="muted small">
              Offizielle Quelle: <SourceLink url={entry.source} />
            </p>
          )}
          <Measured row={row} />
          {finding !== null ? (
            <FindingBody finding={finding} withExplanation={false} />
          ) : (
            isOpen(check) && entry !== undefined && <CustomerCost impact={entry.customerImpact} />
          )}
        </div>
      </details>
      {line !== null && <CheckProgressView line={line} />}
    </li>
  );
}
