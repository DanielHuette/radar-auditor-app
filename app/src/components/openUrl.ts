export async function openInNewTab(resolve: () => Promise<string | null>): Promise<boolean> {
  const tab = window.open("about:blank", "_blank");
  if (tab !== null) tab.opener = null;
  try {
    const url = await resolve();
    if (url === null) {
      tab?.close();
      return false;
    }
    if (tab === null) window.location.assign(url);
    else tab.location.href = url;
    return true;
  } catch (error) {
    tab?.close();
    throw error;
  }
}

export function saveFile(name: string, content: Uint8Array, type: string): void {
  const url = URL.createObjectURL(new Blob([new Uint8Array(content)], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

export async function downloadFile(resolve: () => Promise<string | null>): Promise<boolean> {
  const url = await resolve();
  if (url === null) return false;
  window.location.assign(url);
  return true;
}
