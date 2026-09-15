import { de } from './i18n/de';
import { en } from './i18n/en';
import { es } from './i18n/es';
import { fr } from './i18n/fr';
import { ja } from './i18n/ja';
import { ko } from './i18n/ko';
import { zh } from './i18n/zh';

export type Language = 'en' | 'zh' | 'es' | 'fr' | 'de' | 'ja' | 'ko';

export type Dict = typeof en;

const dictionaries: Record<Language, Record<string, string>> = { en, zh, es, fr, de, ja, ko };

const STORAGE_KEY = 'calliope-lang';

function isLanguage(value: string | null): value is Language {
	return value !== null && Object.hasOwn(dictionaries, value);
}

function initialLanguage(): Language {
	if (typeof localStorage === 'undefined') return 'en';
	// Validate against the registered dictionaries — a stored value from a
	// language this build no longer ships must not blank the UI.
	const stored = localStorage.getItem(STORAGE_KEY);
	return isLanguage(stored) ? stored : 'en';
}

export const language = $state<{ current: Language }>({ current: initialLanguage() });

export function setLanguage(lang: Language) {
	language.current = lang;
	try {
		localStorage.setItem(STORAGE_KEY, lang);
	} catch {
		/* ponytail: storage unavailable (private mode) — UI language just won't persist */
	}
}

/** Look up a UI string. Falls back to English, then to the key itself. */
export function t(key: string, vars?: Record<string, string | number>): string {
	let s: string = dictionaries[language.current][key] ?? (en as Record<string, string>)[key] ?? key;
	if (vars) {
		for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, String(v));
		// ponytail: count 语义占位统一为 {n}；调用方传 count 即命中
		if (vars.count !== undefined) s = s.replaceAll('{n}', String(vars.count));
	}
	return s;
}