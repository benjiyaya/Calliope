import { en } from './i18n/en';
import { zh } from './i18n/zh';

export type Language = 'en' | 'zh';

export type Dict = typeof en;

const dictionaries: Record<Language, Record<string, string>> = { en, zh };

const STORAGE_KEY = 'calliope-lang';

function initialLanguage(): Language {
	if (typeof localStorage === 'undefined') return 'en';
	return localStorage.getItem(STORAGE_KEY) === 'zh' ? 'zh' : 'en';
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

export function toggleLanguage() {
	setLanguage(language.current === 'zh' ? 'en' : 'zh');
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