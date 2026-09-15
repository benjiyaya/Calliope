<script lang="ts">
	import { language, setLanguage, type Language } from '$lib/i18n.svelte';
	import { t } from '$lib/i18n.svelte';
</script>

<select
	class="lang-switcher"
	value={language.current}
	aria-label={t('common.language')}
	onchange={(e) => setLanguage(e.currentTarget.value as Language)}
>
	<option value="de">Deutsch</option>
	<option value="en">English</option>
	<option value="es">Español</option>
	<option value="fr">Français</option>
	<option value="ja">日本語</option>
	<option value="ko">한국어</option>
	<option value="zh">中文</option>
</select>

<style>
	.lang-switcher {
		background: var(--bg-elevated);
		color: var(--text-primary);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 6px 8px;
		font-size: 12px;
		font-family: inherit;
		cursor: pointer;
		min-height: 34px;
	}
	.lang-switcher:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
</style>