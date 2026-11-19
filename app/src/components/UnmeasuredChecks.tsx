import { AREA_NAMES } from "../lib/catalogue";
import type { UnmeasuredCheck } from "../lib/evaluation";

function byArea(checks: readonly UnmeasuredCheck[]): [string, UnmeasuredCheck[]][] {
  const groups = new Map<string, UnmeasuredCheck[]>();
  for (const check of checks) groups.set(check.area, [...(groups.get(check.area) ?? []), check]);
  return [...groups];
}

export function UnmeasuredChecks({ checks }: { checks: readonly UnmeasuredCheck[] }) {
  if (checks.length === 0) return null;
  return (
    <section class="panel" aria-labelledby="unmeasured-title">
      <details>
        <summary class="check__summary">
          <h2 id="unmeasured-title" class="check__name">
            Nicht gemessen ({checks.length})
          </h2>
        </summary>
        {byArea(checks).map(([area, list]) => (
          <div key={area} class="stack stack--tight">
            <h3>{AREA_NAMES[area] ?? `Bereich ${area}`}</h3>
            <ul class="checks">
              {list.map((check) => (
                <li key={check.checkId} class="check unmeasured">
                  <span class="check__name">{check.name}</span>
                  <span class="muted small">{check.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </details>
    </section>
  );
}
