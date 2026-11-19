const UNSAFE = /[^\p{L}\p{N}]+/gu;

function fileName(kind: string, customerName: string, requestedAt: string): string {
  const name = customerName.replace(UNSAFE, "-").replace(/^-+|-+$/g, "");
  const day = requestedAt.slice(0, 10);
  return `${name === "" ? kind : `${kind}-${name}`}-${day}.pdf`;
}

export function reportFileName(customerName: string, requestedAt: string): string {
  return fileName("Audit", customerName, requestedAt);
}

export function estimateFileName(customerName: string, requestedAt: string): string {
  return fileName("Kostenvoranschlag", customerName, requestedAt);
}
