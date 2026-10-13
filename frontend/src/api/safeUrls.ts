export function safeHttpUrl(value: string): string | null {
  try {
    const protocol = new URL(value).protocol;
    return protocol === "http:" || protocol === "https:" ? value : null;
  } catch {
    return null;
  }
}

function isNumericLoopback(hostname: string): boolean {
  if (hostname === "[::1]") return true;
  const octets = hostname.split(".");
  return octets.length === 4
    && octets.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
    && octets[0] === "127";
}

export function safeExternalUrl(value: string): { href: string; hostname: string } | null {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isNumericLoopback(parsed.hostname))) {
      return null;
    }
    return { href: parsed.href, hostname: parsed.hostname };
  } catch {
    return null;
  }
}
