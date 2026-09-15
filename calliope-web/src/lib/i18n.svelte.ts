import { de } from './i18n/de';
import { en } from './i18n/en';
import { es } from './i18n/es';
import { fr } from './i18n/fr';
import { ja } from './i18n/ja';
import { ko } from './i18n/ko';
import { zh } from './i18n/zh';

import { storageGet, storageSet } from './storage';

export type Language = 'en' | 'zh' | 'es' | 'fr' | 'de' | 'ja' | 'ko';

export type Dict = typeof en;

const dictionaries: Record<Language, Record<string, string>> = { en, zh, es, fr, de, ja, ko };

const STORAGE_KEY = 'calliope-lang';

function isLanguage(value: string | null): value is Language {
	return value !== null && Object.hasOwn(dictionaries, value);
}

function initialLanguage(): Language {
	// Validated against the registered dictionaries — a stored value naming a
	// language this build no longer ships must not blank the UI. This runs on
	// the server too, where storage is absent and we render the default.
	const stored = storageGet(STORAGE_KEY);
	return isLanguage(stored) ? stored : 'en';
}

export const language = $state<{ current: Language }>({ current: initialLanguage() });

export function setLanguage(lang: Language) {
	language.current = lang;
	storageSet(STORAGE_KEY, lang);
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