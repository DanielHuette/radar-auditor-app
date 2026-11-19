import { useEffect, useState } from "preact/hooks";
import { getAuditProgress } from "../api/queries";
import type { AuditEstimate, AuditWithCustomer, CheckResult, ManagerEvent } from "../api/types";
import { stepStates, type StepState } from "../lib/checkProgress";
import { controlRunning } from "../lib/checkVerdicts";
import { describeError } from "../lib/errors";
import { checkEventReasons } from "../lib/evaluation";
import { auditProgress, checkEventCount, isFinished, managerEvents, repairLines, routeLines, type Progress, type RepairLine, type RouteLine } from "../lib/progress";
import { followLive } from "./followLive";

export interface AuditProgressState {
  audit: AuditWithCustomer | null | undefined;
  progress: Progress | null;
  manager: ManagerEvent[];
  routes: RouteLine[];
  repairs: RepairLine[];
  checksScored: number;
  estimates: AuditEstimate[];
  checks: CheckResult[];
  states: ReadonlyMap<string, StepState>;
  eventReasons: ReadonlyMap<string, string>;
  error: string | null;
}

const LOADING: AuditProgressState = { audit: undefined, progress: null, manager: [], routes: [], repairs: [], checksScored: 0, estimates: [], checks: [], states: new Map(), eventReasons: new Map(), error: null };

type Loaded = Omit<AuditProgressState, "error">;

async function loadProgress(auditId: string): Promise<Loaded> {
  const { audit, jobs, events, estimates, checks } = await getAuditProgress(auditId);
  return {
    audit,
    progress: audit === null ? null : auditProgress(audit, jobs),
    manager: managerEvents(events),
    routes: routeLines(events),
    repairs: repairLines(events),
    checksScored: checkEventCount(events),
    estimates,
    checks,
    states: stepStates(events),
    eventReasons: checkEventReasons(events),
  };
}

function settled(progress: Progress | null): boolean {
  return progress === null || (isFinished(progress.step) && !progress.serpRetry);
}

export function useAuditProgress(auditId: string | null): AuditProgressState {
  const [state, setState] = useState<AuditProgressState>(LOADING);

  useEffect(() => {
    setState(LOADING);
    if (auditId === null) return;
    const follow = followLive({
      channel: `pruefung-${auditId}`,
      tables: [
        { table: "audits", filter: `id=eq.${auditId}` },
        { table: "jobs", filter: `audit_id=eq.${auditId}` },
        { table: "audit_events", filter: `audit_id=eq.${auditId}` },
        { table: "check_results", filter: `audit_id=eq.${auditId}` },
      ],
      load: () => loadProgress(auditId),
      waiting: ({ progress, checks }) => !settled(progress) || controlRunning(checks),
      onValue: (loaded) => {
        setState({ ...loaded, error: null });
      },
      onError: (reason) => {
        setState((previous) => ({ ...previous, error: describeError(reason) }));
      },
    });
    return follow.stop;
  }, [auditId]);

  return state;
}
