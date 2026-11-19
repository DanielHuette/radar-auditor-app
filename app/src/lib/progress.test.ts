import { describe, expect, it } from "vitest";
import type { AuditEvent, AuditStatus, JobKind, JobStatus, ManagerEvent } from "../api/types";
import {
  analysisWaitText,
  auditProgress,
  formatNextAttempt,
  checkEventCount,
  isFinished,
  MANAGER_VERDICT_LABEL,
  managerEvents,
  managerNote,
  repairLines,
  routeLines,
  stepOfAuditStatus,
  type ProgressJob,
} from "./progress";

function job(
  kind: JobKind,
  status: JobStatus,
  created_at = "2026-10-23T08:00:00Z",
  error: string | null = null,
  run_after: string | null = null,
): ProgressJob {
  return { kind, status, error, created_at, run_after };
}

describe("stepOfAuditStatus", () => {
  it.each<[AuditStatus, string]>([
    ["requested", "waiting"],
    ["collecting", "collecting"],
    ["collected", "scoring"],
    ["scored", "analysing"],
    ["awaiting_analysis", "analysing"],
    ["analysed", "reporting"],
    ["reported", "done"],
    ["failed", "failed"],
    ["halted", "halted"],
  ])("maps %s to %s", (status, step) => {
    expect(stepOfAuditStatus(status)).toBe(step);
  });
});

describe("auditProgress", () => {
  it("waits while the collect job is queued", () => {
    expect(auditProgress({ status: "requested", error: null }, [job("collect", "queued")])).toEqual({
      step: "waiting",
      serpRetry: false,
      analysisWait: null,
      error: null,
    });
  });

  it("shows collecting as soon as the collect job runs, before the audit row changes", () => {
    expect(auditProgress({ status: "requested", error: null }, [job("collect", "running")]).step).toBe("collecting");
  });

  it("never goes back behind the audit status because of an older job", () => {
    expect(auditProgress({ status: "awaiting_analysis", error: null }, [job("collect", "running")]).step).toBe("analysing");
  });

  it("reports the deferred Google search while a serp_retry job is queued", () => {
    const progress = auditProgress({ status: "awaiting_analysis", error: null }, [job("collect", "done"), job("serp_retry", "queued")]);
    expect(progress.serpRetry).toBe(true);
    expect(progress.step).toBe("analysing");
  });

  it("drops the deferred search note once the retry is done", () => {
    expect(auditProgress({ status: "reported", error: null }, [job("serp_retry", "done")]).serpRetry).toBe(false);
  });

  it("fails with the audit error when the audit failed", () => {
    expect(auditProgress({ status: "failed", error: "Website nicht erreichbar" }, [])).toEqual({
      step: "failed",
      serpRetry: false,
      analysisWait: null,
      error: "Website nicht erreichbar",
    });
  });

  it("fails with the job error when the newest job failed", () => {
    const progress = auditProgress({ status: "collecting", error: null }, [
      job("collect", "failed", "2026-10-23T08:05:00Z", "Browser abgestürzt"),
    ]);
    expect(progress.step).toBe("failed");
    expect(progress.error).toBe("Browser abgestürzt");
  });

  it("ignores a failed job that a newer job of the same kind replaced", () => {
    const progress = auditProgress({ status: "collecting", error: null }, [
      job("collect", "failed", "2026-10-23T08:00:00Z", "Zeitüberschreitung"),
      job("collect", "running", "2026-10-23T08:10:00Z"),
    ]);
    expect(progress.step).toBe("collecting");
  });

  it("does not fail a reported audit because a deferred search failed", () => {
    expect(auditProgress({ status: "reported", error: null }, [job("serp_retry", "failed")]).step).toBe("done");
  });

  it("halts with the audit reason and does not fail on an old job", () => {
    const progress = auditProgress({ status: "halted", error: "Claude nicht angemeldet" }, [job("collect", "failed", "2026-10-23T08:05:00Z", "alt")]);
    expect(progress).toEqual({ step: "halted", serpRetry: false, analysisWait: null, error: "Claude nicht angemeldet" });
  });

  it("shows the report step while the report job runs", () => {
    expect(auditProgress({ status: "analysed", error: null }, [job("report", "running")]).step).toBe("reporting");
  });
});

describe("analysis waiting for Claude Code", () => {
  const reason = "Claude Code ist nicht angemeldet";

  it("reports the reason and the next attempt of a re-queued analyse job", () => {
    const progress = auditProgress({ status: "awaiting_analysis", error: null }, [
      job("collect", "done"),
      job("analyse", "queued", "2026-10-23T08:10:00Z", reason, "2026-10-23T08:40:00Z"),
    ]);
    expect(progress.step).toBe("analysing");
    expect(progress.analysisWait).toEqual({ reason, nextAttempt: "2026-10-23T08:40:00Z" });
  });

  it("says nothing while the analyse job runs again or has no reason", () => {
    const running = [job("analyse", "running", "2026-10-23T08:10:00Z", reason, "2026-10-23T08:40:00Z")];
    const fresh = [job("analyse", "queued", "2026-10-23T08:10:00Z")];
    expect(auditProgress({ status: "awaiting_analysis", error: null }, running).analysisWait).toBeNull();
    expect(auditProgress({ status: "awaiting_analysis", error: null }, fresh).analysisWait).toBeNull();
  });

  it("says nothing once the audit moved on", () => {
    const jobs = [job("analyse", "queued", "2026-10-23T08:10:00Z", reason)];
    expect(auditProgress({ status: "analysed", error: null }, jobs).analysisWait).toBeNull();
    expect(auditProgress({ status: "reported", error: null }, jobs).analysisWait).toBeNull();
  });
});

describe("formatNextAttempt", () => {
  const now = new Date(2026, 9, 23, 10, 0);

  it("gives the clock time today and the date on another day", () => {
    expect(formatNextAttempt(new Date(2026, 9, 23, 14, 30).toISOString(), now)).toBe("um 14:30 Uhr");
    expect(formatNextAttempt(new Date(2026, 9, 24, 9, 5).toISOString(), now)).toBe("am 24.10. um 09:05 Uhr");
  });

  it("says gleich when the time has come and nothing without a usable time", () => {
    expect(formatNextAttempt(new Date(2026, 9, 23, 9, 59).toISOString(), now)).toBe("gleich");
    expect(formatNextAttempt(null, now)).toBeNull();
    expect(formatNextAttempt("bald", now)).toBeNull();
  });

  it("builds the waiting line with and without a next attempt", () => {
    const reason = "Claude Code antwortet nicht";
    const later = new Date(2026, 9, 23, 11, 15).toISOString();
    expect(analysisWaitText({ reason, nextAttempt: later }, now)).toBe(
      "Auswertung wartet: Claude Code antwortet nicht – nächster Versuch um 11:15 Uhr",
    );
    expect(analysisWaitText({ reason, nextAttempt: null }, now)).toBe("Auswertung wartet: Claude Code antwortet nicht");
  });
});

describe("isFinished", () => {
  it("is true for done and failed only", () => {
    expect(isFinished("done")).toBe(true);
    expect(isFinished("failed")).toBe(true);
    expect(isFinished("analysing")).toBe(false);
  });
});

function manager(result: ManagerEvent["result"], label: string, reason: string | null): ManagerEvent {
  return { occurred_at: "2026-11-13T09:00:00Z", result, label, reason };
}

describe("managerNote", () => {
  it("shows nothing without events", () => {
    expect(managerNote([])).toBeNull();
  });

  it("does not show a plain note without a problem", () => {
    expect(managerNote([manager("offen", "Audit-Manager", "Sammler neu gestartet")])).toBeNull();
  });

  it.each<[ManagerEvent["result"], string, string]>([
    ["gemessen", "ok", "Audit-Manager: vollständig und plausibel – Alles da"],
    ["ausgesetzt", "plain", "Audit-Manager: noch nicht vollständig – Alles da"],
    ["fehlgeschlagen", "bad", "Audit-Manager: fehlgeschlagen – Alles da"],
  ])("words the verdict %s", (result, tone, text) => {
    expect(managerNote([manager(result, MANAGER_VERDICT_LABEL, " Alles da ")])).toEqual({ tone, text });
  });

  it("words a verdict without a reason by its prefix alone", () => {
    expect(managerNote([manager("gemessen", MANAGER_VERDICT_LABEL, null)])?.text).toBe("Audit-Manager: vollständig und plausibel");
  });

  it("reports a problem note", () => {
    expect(managerNote([manager("fehlgeschlagen", "Audit-Manager", "Bing blockt")])).toEqual({
      tone: "bad",
      text: "Audit-Manager meldet ein Problem: Bing blockt",
    });
  });

  it("shows the newest of verdicts and problems", () => {
    const events = [
      manager("fehlgeschlagen", "Audit-Manager", "Bing blockt"),
      manager("gemessen", MANAGER_VERDICT_LABEL, "Fertig"),
      manager("offen", "Audit-Manager", "Nur eine Notiz"),
    ];
    expect(managerNote(events)?.text).toBe("Audit-Manager: vollständig und plausibel – Fertig");
    expect(managerNote([...events, manager("fehlgeschlagen", "Audit-Manager", "Neuer Fehler")])?.text).toBe(
      "Audit-Manager meldet ein Problem: Neuer Fehler",
    );
  });
});

function event(kind: AuditEvent["kind"], overrides: Partial<AuditEvent>): AuditEvent {
  return { occurred_at: "2026-11-13T09:00:00Z", kind, result: "gemessen", label: "Google-Suche", reason: null, stage: 1, route: "google_search", check_id: null, ...overrides };
}

describe("routeLines", () => {
  it("keeps the newest event per route and sorts by stage", () => {
    const events = [
      event("route", { route: "pagespeed", label: "PageSpeed", stage: 3 }),
      event("route", { route: "assistant:claude", label: "Claude", stage: 1, result: "fehlgeschlagen", reason: "Not logged in" }),
      event("route", { route: "assistant:claude", label: "Claude", stage: 1, result: "gemessen", reason: "  " }),
      event("route", { route: null, label: "Website", stage: null }),
    ];
    expect(routeLines(events)).toEqual([
      { key: "assistant:claude", label: "Claude", result: "gemessen", reason: null, stage: 1 },
      { key: "pagespeed", label: "PageSpeed", result: "gemessen", reason: null, stage: 3 },
      { key: "Website", label: "Website", result: "gemessen", reason: null, stage: null },
    ]);
  });

  it("ignores check and manager events", () => {
    expect(routeLines([event("check", { check_id: "a1" }), event("manager", {})])).toEqual([]);
  });

  it("ignores the events of the audit agent", () => {
    expect(routeLines([event("repair", { result: "Warnung" })])).toEqual([]);
  });

  it("reports the reason of a route that needs Daniel", () => {
    const [line] = routeLines([event("route", { result: "Schritt mit Daniel", reason: "Google zeigt eine Sicherheitsabfrage" })]);
    expect(line).toMatchObject({ result: "Schritt mit Daniel", reason: "Google zeigt eine Sicherheitsabfrage" });
  });
});

describe("event helpers", () => {
  it("counts each scored check once and keeps only manager events", () => {
    const events = [event("check", { check_id: "a1" }), event("check", { check_id: "a1" }), event("check", { check_id: "a2" }), event("manager", {})];
    expect(checkEventCount(events)).toBe(2);
    expect(managerEvents(events)).toHaveLength(1);
  });
});

describe("repairLines", () => {
  it("keeps every restart, takeover and warning of the audit agent in order", () => {
    const events = [
      event("route", {}),
      event("repair", { occurred_at: "2026-11-13T09:00:00Z", result: "neu gestartet", label: "Audit-Agent übernimmt", reason: "Bing-Suche, Versuch 1" }),
      event("repair", { occurred_at: "2026-11-13T09:30:00Z", result: "Warnung", label: "Audit-Agent: Teil scheitert weiter", reason: " " }),
    ];
    expect(repairLines(events)).toEqual([
      { key: "2026-11-13T09:00:00Z-0", time: "2026-11-13T09:00:00Z", label: "Audit-Agent übernimmt", result: "neu gestartet", reason: "Bing-Suche, Versuch 1" },
      { key: "2026-11-13T09:30:00Z-1", time: "2026-11-13T09:30:00Z", label: "Audit-Agent: Teil scheitert weiter", result: "Warnung", reason: null },
    ]);
  });
});

describe("a running repair", () => {
  it("counts as collecting", () => {
    expect(auditProgress({ status: "collecting", error: null }, [job("collect", "done"), job("repair", "queued")]).step).toBe("collecting");
  });
});
