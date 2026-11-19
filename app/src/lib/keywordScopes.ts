import type { Keyword, KeywordScope, Ranking } from "../api/types";
import { isCustomer } from "./rankings";

export interface KeywordLine {
  id: string;
  phrase: string;
  volume: number | null;
  measured: boolean;
  organic: number | null;
  local: number | null;
}

export interface KeywordScopeGroup {
  scope: KeywordScope;
  title: string;
  lines: KeywordLine[];
}

const SCOPE_TITLES: Record<KeywordScope, string> = {
  local: "Örtliche Suchbegriffe",
  nationwide: "Bundesweite Suchbegriffe",
};

const SCOPES: readonly KeywordScope[] = ["local", "nationwide"];

function best(values: readonly (number | null)[]): number | null {
  const found = values.filter((value): value is number => value !== null);
  return found.length === 0 ? null : Math.min(...found);
}

function lineOf(keyword: Keyword, rankings: readonly Ranking[], customerName: string): KeywordLine {
  const plain = rankings.filter((ranking) => ranking.keyword_id === keyword.id && ranking.grid_point === null);
  const own = plain.filter((ranking) => isCustomer(ranking.subject, customerName));
  return {
    id: keyword.id,
    phrase: keyword.phrase,
    volume: keyword.monthly_volume,
    measured: plain.length > 0,
    organic: best(own.map((ranking) => ranking.organic_position)),
    local: best(own.map((ranking) => ranking.local_pack_position)),
  };
}

function byDemand(left: KeywordLine, right: KeywordLine): number {
  return (right.volume ?? -1) - (left.volume ?? -1) || left.phrase.localeCompare(right.phrase, "de");
}

export function keywordScopes(keywords: readonly Keyword[], rankings: readonly Ranking[], customerName: string): KeywordScopeGroup[] {
  return SCOPES.map((scope) => ({
    scope,
    title: SCOPE_TITLES[scope],
    lines: keywords
      .filter((keyword) => keyword.scope === scope)
      .map((keyword) => lineOf(keyword, rankings, customerName))
      .sort(byDemand),
  }));
}
