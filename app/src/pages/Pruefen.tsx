import { useLocation } from "preact-iso";
import { useEffect, useId, useRef, useState } from "preact/hooks";
import { startCustomerAudit, startFieldAudit } from "../api/queries";
import type { Customer } from "../api/types";
import { AuditProgress } from "../components/AuditProgress";
import { CheckSelection } from "../components/CheckSelection";
import { CustomerPicker } from "../components/CustomerPicker";
import { Icon } from "../components/Icon";
import { OpenRequests } from "../components/OpenRequests";
import { RadarBackdrop } from "../components/RadarBackdrop";
import { useServerStatus } from "../hooks/serverStatus";
import { useAction } from "../hooks/useAsync";
import { CATALOGUE_AREAS } from "../lib/catalogue";
import { deviceStorage, loadSelection, saveSelection } from "../lib/checkSelection";
import { quickAuditRequest, validateWebsite } from "../lib/fieldAudit";
import { hostOf } from "../lib/format";

interface Target {
  label: string;
  start: (disabledChecks: readonly string[]) => Promise<string>;
}

function websiteTarget(website: string): Target {
  return {
    label: hostOf(website).replace(/^www\./, ""),
    start: (disabledChecks) => startFieldAudit(quickAuditRequest(website, disabledChecks)),
  };
}

function customerTarget(customer: Customer): Target {
  return { label: customer.name, start: (disabledChecks) => startCustomerAudit(customer.id, disabledChecks) };
}

interface UrlInputProps {
  value: string;
  errorId: string | null;
  onInput: (value: string) => void;
}

function UrlInput({ value, errorId, onInput }: UrlInputProps) {
  return (
    <label class="url-field">
      <span class="url-field__icon" aria-hidden="true">
        <Icon name="link" />
      </span>
      <input
        class="url-field__input"
        type="text"
        inputMode="url"
        autoCapitalize="off"
        autoComplete="url"
        spellcheck={false}
        placeholder="meisterbetrieb-muster.de"
        aria-label="Website des Betriebs"
        aria-invalid={errorId !== null}
        aria-describedby={errorId ?? undefined}
        value={value}
        onInput={(event) => {
          onInput(event.currentTarget.value);
        }}
      />
    </label>
  );
}

interface AddressStepProps {
  initial: string;
  focusGo: boolean;
  onNext: (website: string, typed: string) => void;
}

function AddressStep({ initial, focusGo, onNext }: AddressStepProps) {
  const [text, setText] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const errorId = useId();
  const go = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (focusGo) go.current?.focus();
  }, [focusGo]);
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    const result = validateWebsite(text);
    setError(result.ok ? null : result.error);
    if (result.ok) onNext(result.website, text);
  };
  return (
    <form class="start" noValidate onSubmit={submit}>
      <UrlInput value={text} errorId={error === null ? null : errorId} onInput={setText} />
      {error !== null && (
        <p id={errorId} class="start__error field__error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" class="btn btn--primary btn--main start__go" ref={go}>
        Prüfen
      </button>
    </form>
  );
}

function useStoredSelection(): [string[], (next: string[]) => void] {
  const [disabled, setDisabled] = useState<string[]>(() => loadSelection(deviceStorage()));
  const change = (next: string[]) => {
    setDisabled(next);
    saveSelection(deviceStorage(), next);
  };
  return [disabled, change];
}

function StartNotes({ blockedReason, error }: { blockedReason: string | null; error: string | null }) {
  return (
    <>
      {blockedReason !== null && (
        <p class="notice notice--bad" role="status">
          Prüfungen gerade nicht möglich: {blockedReason}
        </p>
      )}
      {error !== null && (
        <p class="field__error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

interface SelectStepProps {
  target: Target;
  onBack: () => void;
  onStarted: (auditId: string) => void;
}

function SelectStep({ target, onBack, onStarted }: SelectStepProps) {
  const [disabled, setDisabled] = useStoredSelection();
  const action = useAction();
  const blockedReason = useServerStatus()?.blockedReason ?? null;
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    if (blockedReason !== null) return;
    void action.run(async () => {
      onStarted(await target.start(disabled));
    });
  };
  return (
    <form class="choose" noValidate onSubmit={submit}>
      <button type="button" class="address-label" aria-label={`${target.label} ändern`} onClick={onBack}>
        {target.label}
      </button>
      <CheckSelection areas={CATALOGUE_AREAS} disabled={disabled} onChange={setDisabled} />
      <StartNotes blockedReason={blockedReason} error={action.error} />
      <div class="choose__go">
        <button type="submit" class="btn btn--primary btn--main btn--long" disabled={action.busy || blockedReason !== null}>
          {action.busy ? "Wird gestartet …" : "Prüfung starten"}
        </button>
      </div>
    </form>
  );
}

function ProgressStep({ auditId, onNew }: { auditId: string; onNew: () => void }) {
  return (
    <div class="progress-page">
      <AuditProgress auditId={auditId} onNew={onNew} />
    </div>
  );
}

function useTypedAddress() {
  const [typed, setTyped] = useState("");
  const [picked, setPicked] = useState(0);
  const pick = (next: string) => {
    setTyped(next);
    setPicked((count) => count + 1);
  };
  return { typed, setTyped, picked, pick };
}

interface StartStepProps {
  address: ReturnType<typeof useTypedAddress>;
  onNext: (website: string, typed: string) => void;
  onCustomer: (customer: Customer) => void;
}

function StartStep({ address, onNext, onCustomer }: StartStepProps) {
  return (
    <>
      <AddressStep key={address.picked} initial={address.typed} focusGo={address.picked > 0} onNext={onNext} />
      <div class="requests-layer">
        <OpenRequests onPick={address.pick} />
      </div>
      <CustomerPicker onPick={onCustomer} />
    </>
  );
}

interface StepProps {
  auditId: string | null;
  target: Target | null;
  address: ReturnType<typeof useTypedAddress>;
  setTarget: (target: Target | null) => void;
  showAudit: (id: string | null) => void;
}

function PreparingStep({ target, address, setTarget, showAudit }: Omit<StepProps, "auditId">) {
  if (target !== null) {
    return (
      <SelectStep
        target={target}
        onBack={() => {
          setTarget(null);
        }}
        onStarted={showAudit}
      />
    );
  }
  const enterAddress = (website: string, text: string) => {
    address.setTyped(text);
    setTarget(websiteTarget(website));
  };
  return (
    <StartStep
      address={address}
      onNext={enterAddress}
      onCustomer={(customer) => {
        setTarget(customerTarget(customer));
      }}
    />
  );
}

function CurrentStep({ auditId, ...rest }: StepProps) {
  if (auditId === null) return <PreparingStep {...rest} />;
  return (
    <ProgressStep
      auditId={auditId}
      onNew={() => {
        rest.showAudit(null);
      }}
    />
  );
}

export function Pruefen() {
  const { query, route } = useLocation();
  const auditId = query.pruefung ?? null;
  const address = useTypedAddress();
  const [target, setTarget] = useState<Target | null>(null);
  const showAudit = (id: string | null) => {
    setTarget(null);
    route(id === null ? "/" : `/?pruefung=${id}`);
  };
  return (
    <>
      <RadarBackdrop faint={auditId === null && target !== null} />
      <CurrentStep auditId={auditId} target={target} address={address} setTarget={setTarget} showAudit={showAudit} />
    </>
  );
}
