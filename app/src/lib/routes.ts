const PAID_ROUTES: Readonly<Record<string, string>> = {
  dataforseo: "DataForSEO",
  apify: "Apify",
  places: "Google-Schnittstelle",
  places_api: "Google-Schnittstelle",
  openai: "OpenAI-Schnittstelle",
  openai_api: "OpenAI-Schnittstelle",
};

function routeIds(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(routeIds);
  if (typeof value !== "object" || value === null) return [];
  if ("route" in value) return routeIds(value.route);
  return Object.values(value).flatMap(routeIds);
}

function joinGerman(parts: readonly string[]): string {
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} und ${parts.at(-1) ?? ""}`;
}

export function paidRouteText(measured: Readonly<Record<string, unknown>>): string | null {
  const ids = [...routeIds(measured.route), ...routeIds(measured.routes)];
  const labels = [...new Set(ids.flatMap((id) => PAID_ROUTES[id.trim().toLowerCase()] ?? []))];
  return labels.length === 0 ? null : `über ${joinGerman(labels)}`;
}
