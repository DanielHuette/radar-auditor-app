import { useId } from "preact/hooks";
import { useTilt } from "../hooks/motion";

function MarkDefs({ gold, depth }: { gold: string; depth: string }) {
  return (
    <defs>
      <linearGradient id={gold} x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#f3e3bf" />
        <stop offset="0.32" stop-color="#cdb07a" />
        <stop offset="0.55" stop-color="#9c7f52" />
        <stop offset="0.78" stop-color="#e2c993" />
        <stop offset="1" stop-color="#8a6c43" />
      </linearGradient>
      <filter id={depth} x="-20%" y="-20%" width="150%" height="150%">
        <feDropShadow dx="1.6" dy="2.2" stdDeviation="1.4" flood-color="#000" flood-opacity="0.65" />
      </filter>
    </defs>
  );
}

export function Mark({ size = 40 }: { size?: number }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg class="mark" width={size} height={size} viewBox="0 0 100 100" aria-hidden="true" focusable="false">
      <MarkDefs gold={`gold-${id}`} depth={`depth-${id}`} />
      <g fill={`url(#gold-${id})`} filter={`url(#depth-${id})`}>
        <path fill-rule="evenodd" d="M50 2.5a47.5 47.5 0 1 1 0 95a47.5 47.5 0 1 1 0-95zm0 5a42.5 42.5 0 1 0 0 85a42.5 42.5 0 1 0 0-85z" />
        <rect x="31.8" y="46.3" width="8.8" height="18" />
        <rect x="47.1" y="24.9" width="8.8" height="39.4" />
        <rect x="62.6" y="36.6" width="8.8" height="27.7" />
        <rect x="12.4" y="64.9" width="75.2" height="4.4" />
      </g>
    </svg>
  );
}

export function Wordmark({ tagline = true }: { tagline?: boolean }) {
  return (
    <span class="wordmark">
      <span class="wordmark__name">REPOCITY</span>
      {tagline && <span class="wordmark__tag">AI FOR EVERYONE</span>}
    </span>
  );
}

interface LogoProps {
  size?: number;
  tagline?: boolean;
  label?: string;
}

export function Logo({ size = 40, tagline = true, label }: LogoProps) {
  return (
    <span class="logo" role="img" aria-label="RepoCity – AI for everyone">
      <Mark size={size} />
      {label === undefined ? (
        <Wordmark tagline={tagline} />
      ) : (
        <span class="logo__words">
          <Wordmark tagline={false} />
          <small class="logo__label">{label}</small>
        </span>
      )}
    </span>
  );
}

export function Lockup() {
  const lockup = useTilt<HTMLDivElement>(18, -8, 9);
  return (
    <div class="lockup" ref={lockup} aria-label="RepoCity – AI for everyone" role="img">
      <Mark size={160} />
      <Wordmark />
    </div>
  );
}
