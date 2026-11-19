import { openInNewTab } from "./openUrl";

const SCHEME = /^https?:\/\/(www\.)?/;

export function SourceLink({ url }: { url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        event.preventDefault();
        void openInNewTab(() => Promise.resolve(url));
      }}
    >
      {url.replace(SCHEME, "")}
    </a>
  );
}
