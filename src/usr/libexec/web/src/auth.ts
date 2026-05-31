// Remote-access auth token. PAI.app's "Enable remote access" toggle mints a
// short-lived code and encodes it into a QR of `https://<host>/?token=<TOK>`.
// On the local unix-socket surface and in dev there is no token and everything
// stays unauthenticated — the backend only enforces when started with
// `--auth-token`, so an absent token here is the normal local case.
//
// Flow: on first load we lift `?token=` out of the URL, persist it to
// localStorage, and strip it from the address bar (so it isn't bookmarked or
// shoulder-surfed). Later loads fall back to the stored value. The "password"
// path (login overlay) writes the same key via `setAuthToken`.

const STORAGE_KEY = "pai.authToken";

let token: string | null = readInitialToken();
let unauthorizedListener: (() => void) | null = null;

function readInitialToken(): string | null {
  try {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("token");
    if (fromUrl) {
      localStorage.setItem(STORAGE_KEY, fromUrl);
      url.searchParams.delete("token");
      const clean = url.pathname + url.search + url.hash;
      window.history.replaceState({}, "", clean);
      return fromUrl;
    }
  } catch {
    // Non-browser or malformed URL — fall through to stored value.
  }
  return localStorage.getItem(STORAGE_KEY);
}

export function getAuthToken(): string | null {
  return token;
}

export function setAuthToken(value: string | null): void {
  const next = value && value.trim() ? value.trim() : null;
  token = next;
  if (next) localStorage.setItem(STORAGE_KEY, next);
  else localStorage.removeItem(STORAGE_KEY);
}

/** Bearer header for fetch()-based calls; empty when there's no token. */
export function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Append `?token=` for transports that can't set headers (EventSource). */
export function withTokenParam(path: string): string {
  if (!token) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}token=${encodeURIComponent(token)}`;
}

/** Register the login-overlay trigger; fired when any API call 401s. */
export function onUnauthorized(fn: (() => void) | null): void {
  unauthorizedListener = fn;
}

export function notifyUnauthorized(): void {
  unauthorizedListener?.();
}
