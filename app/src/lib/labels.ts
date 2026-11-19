import type { AuditKind, AuditStatus, CheckOutcome, Finding, JobStatus } from "../api/types";

export const AUDIT_STATUS_LABELS: Record<AuditStatus, string> = {
  requested: "Angefordert",
  collecting: "Sammelt Daten",
  collected: "Daten gesammelt",
  scored: "Bewertet",
  controlling: "Kontrolle läuft",
  awaiting_analysis: "Wartet auf Auswertung",
  analysed: "Ausgewertet",
  reported: "Bericht fertig",
  failed: "Fehlgeschlagen",
  halted: "Angehalten",
};

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Wartet",
  running: "Läuft",
  done: "Erledigt",
  failed: "Fehlgeschlagen",
};

export const AUDIT_KIND_LABELS: Record<AuditKind, string> = {
  remote: "Fernaudit",
  with_access: "Mit Zugang",
};

export const OUTCOME_LABELS: Record<CheckOutcome, string> = {
  passed: "bestanden",
  partial: "teilweise",
  failed: "nicht bestanden",
  not_measurable: "nicht gemessen",
  skipped: "übersprungen",
  not_applicable: "nicht zutreffend",
};

export const CONTROL_RUNNING_LABEL = "Kontrolle läuft";
export const INFO_LABEL = "Feststellung";
export const AI_NOTICE_LABEL = "Die Texte zu Befunden, Auswertung und Empfehlungen wurden mit künstlicher Intelligenz erstellt.";

export const OUTCOME_TONES: Record<CheckOutcome, string> = {
  passed: "tone-ok",
  partial: "tone-warn",
  failed: "tone-bad",
  not_measurable: "",
  skipped: "",
  not_applicable: "",
};

export const URGENCY: Record<Finding["severity"], { label: string; tone: string }> = {
  3: { label: "Dringend", tone: "tone-bad" },
  2: { label: "Wichtig", tone: "tone-warn" },
  1: { label: "Verbesserung", tone: "" },
};
