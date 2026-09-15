#!/usr/bin/env node
/**
 * Regression guard for the "localStorage.getItem is not a function" SSR crash.
 *
 * Node 22+ exposes a global `localStorage`, and when it is enabled without a
 * usable `--localstorage-file` the binding is a plain object with no Storage
 * methods. `typeof localStorage === 'undefined'` therefore evaluates false, the
 * next call throws, and every server render dies. It only surfaces at module or
 * component scope, because that is what SvelteKit also evaluates on the server.
 *
 * Two layers of protection:
 *   1. behavioural — the helper must degrade to null under every storage shape
 *      the server can present, and still round-trip against a real Storage.
 *   2. static — the unsafe `typeof localStorage` idiom is banned outside the
 *      helper, so the crash cannot be reintroduced by copying the old pattern.
 *
 * Run with `npm test` (or `node scripts/check-storage-ssr.mjs`).
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const SRC = join(ROOT, 'src');
const HELPER = join(SRC, 'lib', 'storage.ts');

let failures = 0;

function fail(message) {
	failures += 1;
	console.error(`FAIL  ${message}`);
}

function ok(message) {
	console.log(`ok    ${message}`);
}

/** Replace the global under test. Node's own binding may be getter-only. */
function setGlobalStorage(value) {
	Object.defineProperty(globalThis, 'localStorage', {
		value,
		configurable: true,
		writable: true,
	});
}

/** A Storage that behaves like a browser's. */
function fakeStorage(initial = {}) {
	const map = new Map(Object.entries(initial));
	return {
		getItem: (k) => (map.has(k) ? map.get(k) : null),
		setItem: (k, v) => void map.set(k, String(v)),
		removeItem: (k) => void map.delete(k),
		clear: () => map.clear(),
		key: (i) => [...map.keys()][i] ?? null,
		get length() {
			return map.size;
		},
	};
}

const { storageGet, storageSet, storageRemove } = await import(pathToFileURL(HELPER).href);

// ---- 1. behavioural ----------------------------------------------------

// The exact shape Node presents on the server: an object with no Storage methods.
// This is the configuration that produced the original crash.
setGlobalStorage({});
try {
	if (storageGet('calliope-lang') !== null) fail('method-less localStorage: expected null');
	else if (storageSet('calliope-lang', 'ja') !== undefined) fail('method-less localStorage: setItem should no-op');
	else {
		storageRemove('calliope-lang');
		ok('method-less localStorage (Node SSR shape) degrades to null without throwing');
	}
} catch (err) {
	fail(`method-less localStorage threw: ${err.message}`);
}

// Absent entirely (the common server case).
setGlobalStorage(undefined);
try {
	if (storageGet('k') !== null) fail('undefined localStorage: expected null');
	else ok('absent localStorage degrades to null');
} catch (err) {
	fail(`undefined localStorage threw: ${err.message}`);
}

// Access itself can throw (sandboxed frame / blocked third-party storage).
setGlobalStorage(
	new Proxy(
		{},
		{
			get() {
				throw new Error('SecurityError');
			},
		},
	),
);
try {
	if (storageGet('k') !== null) fail('throwing localStorage: expected null');
	else ok('throwing localStorage access degrades to null');
} catch (err) {
	fail(`throwing localStorage access threw: ${err.message}`);
}

// Quota / private-mode errors on write must not escape.
setGlobalStorage({
	getItem: () => null,
	setItem: () => {
		throw new Error('QuotaExceededError');
	},
	removeItem: () => {},
});
try {
	storageSet('k', 'v');
	ok('setItem failure is swallowed');
} catch (err) {
	fail(`setItem failure escaped: ${err.message}`);
}

// A real browser Storage must still work — guards against a helper that just
// returns null unconditionally to satisfy the checks above.
setGlobalStorage(fakeStorage());
try {
	storageSet('calliope-lang', 'ko');
	const read = storageGet('calliope-lang');
	if (read !== 'ko') fail(`working storage round-trip: expected "ko", got ${JSON.stringify(read)}`);
	else {
		storageRemove('calliope-lang');
		if (storageGet('calliope-lang') !== null) fail('working storage remove: expected null');
		else ok('working storage round-trips and removes');
	}
} catch (err) {
	fail(`working storage threw: ${err.message}`);
}

// ---- 2. static ---------------------------------------------------------

const UNSAFE = /typeof\s+localStorage/;
const EXTENSIONS = ['.ts', '.js', '.svelte'];

function walk(dir) {
	const found = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const full = join(dir, entry.name);
		if (entry.isDirectory()) {
			if (entry.name === 'node_modules') continue;
			found.push(...walk(full));
		} else if (EXTENSIONS.some((ext) => entry.name.endsWith(ext))) {
			found.push(full);
		}
	}
	return found;
}

function exists(p) {
	try {
		return statSync(p).isFile();
	} catch {
		return false;
	}
}

const offenders = [];
for (const file of walk(SRC)) {
	if (file === HELPER) continue; // the one place allowed to probe the global
	const lines = readFileSync(file, 'utf8').split('\n');
	lines.forEach((line, i) => {
		if (UNSAFE.test(line)) offenders.push(`${relative(ROOT, file)}:${i + 1}: ${line.trim()}`);
	});
}

if (offenders.length) {
	fail(
		`unsafe \`typeof localStorage\` guard outside lib/storage.ts — use storageGet/storageSet instead:\n  ${offenders.join('\n  ')}`,
	);
} else {
	ok('no unsafe `typeof localStorage` guards in src/');
}

if (!exists(HELPER)) fail('src/lib/storage.ts is missing');

// ---- result ------------------------------------------------------------

if (failures) {
	console.error(`\n${failures} storage/SSR check(s) failed.`);
	process.exit(1);
}
console.log('\nstorage SSR checks passed.');
