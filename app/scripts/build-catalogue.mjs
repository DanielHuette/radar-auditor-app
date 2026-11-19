import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import yaml from "js-yaml";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(root, "..", "config", "pruefkatalog.yaml");
const target = join(root, "src", "generated", "catalogue.json");
const stepsSource = join(root, "..", "config", "schritte.json");
const limitsSource = join(root, "..", "config", "ampel.yaml");
const limitsTarget = join(root, "src", "generated", "ampel.json");
const controlSource = join(root, "..", "config", "kontrolle.yaml");
const controlTarget = join(root, "src", "generated", "control.json");

const routesSource = join(root, "..", "config", "messwege.yaml");
const routesTarget = join(root, "src", "generated", "routes.json");

const KINDS = ["kriterium", "ergebnis", "info"];

function flag(record, key, where) {
  const value = record[key];
  if (typeof value !== "boolean") throw new Error(`${where}: "${key}" must be true or false`);
  return value;
}

function required(record, key, where) {
  const value = record[key];
  if (value === undefined || value === null || value === "") throw new Error(`${where}: "${key}" is missing`);
  return value;
}

function kindOf(check, where) {
  const kind = String(required(check, "kind", where));
  if (!KINDS.includes(kind)) throw new Error(`${where}: "kind" must be one of ${KINDS.join(", ")}`);
  return kind;
}

function sourceOf(check, kind, where) {
  if (kind === "kriterium") return String(required(check, "source", where));
  return check.source === undefined || check.source === null ? "" : String(check.source);
}

function weightOf(areaWeight, counted, kind) {
  if (kind === "info") return 0;
  return Math.round((areaWeight / counted) * 100) / 100;
}

const steps = JSON.parse(await readFile(stepsSource, "utf8"));
const limits = yaml.load(await readFile(limitsSource, "utf8"));
for (const key of ["green_from", "yellow_from"]) {
  if (typeof limits?.[key] !== "number") throw new Error(`${limitsSource}: "${key}" must be a number`);
}

const control = yaml.load(await readFile(controlSource, "utf8"));
for (const key of ["planned_checks", "planned_reasons"]) {
  if (!Array.isArray(control?.[key])) throw new Error(`${controlSource}: "${key}" must be a list`);
}
if (typeof control.invalid_reason !== "string") throw new Error(`${controlSource}: "invalid_reason" must be text`);

function stepsOf(id, where) {
  const found = steps[id];
  if (!Array.isArray(found)) throw new Error(`${where}: no entry in ${stepsSource}`);
  return found.map(String);
}

const parsed = yaml.load(await readFile(source, "utf8"));
if (!Array.isArray(parsed?.areas)) throw new Error(`${source}: "areas" is not a list`);

const areas = parsed.areas.map((area) => {
  const where = `area ${String(area.id)}`;
  if (!Array.isArray(area.checks) || area.checks.length === 0) throw new Error(`${where}: "checks" is not a list`);
  const weight = Number(required(area, "weight", where));
  const counted = area.checks.filter((check) => check.kind !== "info").length;
  return {
    id: String(required(area, "id", where)),
    name: String(required(area, "name", where)),
    weight,
    checks: area.checks.map((check) => {
      const at = `check ${String(check.id)} in ${where}`;
      const kind = kindOf(check, at);
      return {
        id: String(required(check, "id", at)),
        name: String(required(check, "name", at)),
        why: String(required(check, "why", at)),
        area: String(area.id),
        provider: String(required(check, "provider", at)),
        kind,
        source: sourceOf(check, kind, at),
        measurement: String(required(check, "measurement", at)),
        steps: stepsOf(String(check.id), at),
        weight: weightOf(weight, counted, kind),
        requiresPaid: flag(check, "requires_paid", at),
        requiresAccess: flag(check, "requires_access", at),
        customerQuestion: String(required(check, "customer_question", at)),
        customerImpact: String(required(check, "customer_impact", at)),
      };
    }),
  };
});

function routesOf(list, suppliers, where) {
  if (!Array.isArray(list)) throw new Error(`${where}: routes must be a list`);
  return list.map((route) => {
    const supplier = String(required(route, "supplier", where));
    if (typeof suppliers[supplier] !== "string") throw new Error(`${where}: supplier "${supplier}" is not listed under suppliers`);
    const built = flag(route, "built", `${where} ${supplier}`);
    return { supplier, active: built && route.bestaetigt !== false };
  });
}

function waysOf(measurements, suppliers) {
  const ways = [];
  for (const [key, measurement] of Object.entries(measurements)) {
    const label = String(required(measurement, "label", `measurement ${key}`));
    ways.push({ key, label, routes: routesOf(measurement.routes ?? [], suppliers, `measurement ${key}`) });
    for (const [variant, list] of Object.entries(measurement.variants ?? {})) {
      ways.push({ key: `${key}.${variant}`, label, routes: routesOf(list, suppliers, `measurement ${key}.${variant}`) });
    }
  }
  return ways.filter((way) => way.routes.length > 0);
}

const routes = yaml.load(await readFile(routesSource, "utf8"));
if (typeof routes?.suppliers !== "object" || typeof routes?.measurements !== "object") {
  throw new Error(`${routesSource}: "suppliers" and "measurements" are required`);
}

await mkdir(dirname(target), { recursive: true });
await writeFile(
  routesTarget,
  `${JSON.stringify({ suppliers: routes.suppliers, ways: waysOf(routes.measurements, routes.suppliers) }, null, 2)}\n`,
);
await writeFile(target, `${JSON.stringify({ areas }, null, 2)}\n`);
await writeFile(
  controlTarget,
  `${JSON.stringify({ invalidReason: control.invalid_reason, plannedChecks: control.planned_checks.map(String), plannedReasons: control.planned_reasons.map(String) }, null, 2)}\n`,
);
await writeFile(limitsTarget, `${JSON.stringify({ greenFrom: limits.green_from, yellowFrom: limits.yellow_from }, null, 2)}\n`);
