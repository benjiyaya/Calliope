/**
 * SSR-safe `localStorage` access.
 *
 * `typeof localStorage === 'undefined'` is NOT a sufficient guard on modern
 * Node. Node 22+ ships a web-storage global, and when it is enabled without a
 * usable `--localstorage-file` the binding exists but is not a `Storage`: the
 * typeof check passes and the next call throws
 * `localStorage.getItem is not a function`. Because SvelteKit evaluates module
 * and component scope on the server, that turns a page load into a 500.
 *
 * Probe for a callable method instead of trusting the binding to exist.
 */
function webStorage(): Storage | null {
	try {
		if (typeof localStorage === 'undefined') return null;
		return typeof localStorage.getItem === 'function' ? localStorage : null;
	} catch {
		// Reading the global itself can throw (sandboxed frame, blocked storage).
		return null;
	}
}

export function storageGet(key: string): string | null {
	try {
		return webStorage()?.getItem(key) ?? null;
	} catch {
		return null;
	}
}

export function storageSet(key: string, value: string): void {
	try {
		webStorage()?.setItem(key, value);
	} catch {
		// Private mode / quota exceeded — persistence is best-effort.
	}
}

export function storageRemove(key: string): void {
	try {
		webStorage()?.removeItem(key);
	} catch {
		// Best-effort.
	}
}
