import type { Keyword, Ranking } from "../api/types";
import { formatNumber } from "../lib/format";
import { keywordScopes, type KeywordLine } from "../lib/keywordScopes";
import { POSITION_CUTOFF } from "../lib/rankings";
import { Empty } from "./ui";

function placeText(position: number | null, cutoff: number): string {
  return position === null ? `nicht unter den ersten ${String(cutoff)}` : `Platz ${String(position)}`;
}

function KeywordItem({ line }: { line: KeywordLine }) {
  return (
    <li class="keyword">
      <span class="keyword__phrase">{line.phrase}</span>
      <span class="keyword__meta muted small">
        {line.volume === null ? "Nachfrage unbekannt" : `${formatNumber(line.volume)} Suchen im Monat`}
      </span>
      {line.measured ? (
        <span class="keyword__places small">
          Google-Suche: {placeText(line.organic, POSITION_CUTOFF.organic)} · Karte: {placeText(line.local, POSITION_CUTOFF.local)}
        </span>
      ) : (
        <span class="keyword__places muted small">In dieser Prüfung nicht gemessen</span>
      )}
    </li>
  );
}

export function KeywordScopes({ keywords, rankings, customerName }: { keywords: readonly Keyword[]; rankings: readonly Ranking[]; customerName: string }) {
  const groups = keywordScopes(keywords, rankings, customerName);
  return (
    <>
      {groups.map((group) => (
        <section key={group.scope} class="panel" aria-label={group.title}>
          <h2>{group.title}</h2>
          {group.lines.length === 0 ? (
            <Empty>Keine Suchbegriffe dieser Art angelegt.</Empty>
          ) : (
            <ul class="keywords">
              {group.lines.map((line) => (
                <KeywordItem key={line.id} line={line} />
              ))}
            </ul>
          )}
        </section>
      ))}
    </>
  );
}
