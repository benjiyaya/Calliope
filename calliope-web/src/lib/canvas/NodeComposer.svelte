<script lang="ts">
	import { untrack } from 'svelte';
	import { t } from '$lib/i18n.svelte';

	export interface ComposerReference {
		kind: 'image' | 'audio' | 'video';
		url: string;
		name?: string;
	}

	interface Props {
		/** Seed text from the card's saved prompt or its source job. */
		prompt: string;
		mediaUrl: string | null;
		canRegenerate: boolean;
		references?: ComposerReference[];
		/** When set, Regenerate opens the page-level review card instead of an in-card panel. */
		onRequestReview?: (prompt: string) => void;
		onPreview: () => void;
		onRegenerate: (prompt: string) => void | Promise<void>;
		onSendToAgent: (prompt: string) => void;
	}

	let {
		prompt,
		mediaUrl,
		canRegenerate,
		references = [],
		onRequestReview,
		onPreview,
		onRegenerate,
		onSendToAgent,
	}: Props = $props();

	let text = $state(untrack(() => prompt));
	let busy = $state(false);
	let reviewing = $state(false);

	function openReview() {
		const next = text.trim();
		if (!next || !canRegenerate) return;
		if (onRequestReview) {
			onRequestReview(next);
			return;
		}
		reviewing = true;
	}

	async function regenerate() {
		const next = text.trim();
		if (!next || !canRegenerate || busy) return;
		busy = true;
		try {
			await onRegenerate(next);
			reviewing = false;
		} finally {
			busy = false;
		}
	}
</script>

<div class="composer nodrag nopan nowheel">
	<label>
		<span class="label">{t('canvas.prompt')}</span>
		<textarea
			rows={reviewing ? 8 : 3}
			bind:value={text}
			aria-label={t('canvas.prompt')}
			placeholder={t('canvas.prompt')}
		></textarea>
	</label>
	<div class="controls">
		<button
			type="button"
			class="ctrl"
			disabled={!mediaUrl}
			onclick={onPreview}
			title={t('canvas.openPreview')}
		>
			{t('canvas.preview')}
		</button>
		{#if mediaUrl}
			<a class="ctrl" href={mediaUrl} download>{t('canvas.download')}</a>
		{:else}
			<button type="button" class="ctrl" disabled>{t('canvas.download')}</button>
		{/if}
		{#if reviewing}
			<div class="refs">
				{#if mediaUrl}
					{#if mediaUrl.match(/\.(mp4|webm|mov)(\?|#|$)/i)}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video controls src={mediaUrl}></video>
					{:else}
						<img src={mediaUrl} alt="" />
					{/if}
				{/if}
				{#each references as ref (`${ref.kind}-${ref.url}`)}
					{#if ref.kind === 'audio'}
						<audio controls src={ref.url}></audio>
					{:else if ref.kind === 'video'}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video controls src={ref.url}></video>
					{:else}
						<img src={ref.url} alt={ref.name ?? ''} />
					{/if}
				{/each}
			</div>
			<button type="button" class="ctrl" disabled={busy} onclick={() => (reviewing = false)}>
				{t('agentChat.revise')}
			</button>
			<button
				type="button"
				class="ctrl primary"
				disabled={busy || text.trim().length === 0}
				onclick={regenerate}
			>
				{t('agentChat.generateNow')}
			</button>
		{:else if canRegenerate}
			<button
				type="button"
				class="ctrl primary"
				disabled={busy || text.trim().length === 0}
				onclick={openReview}
				title={t('canvas.regenerate')}
			>
				{busy ? t('canvas.running') : t('canvas.regenerate')}
			</button>
		{:else}
			<button
				type="button"
				class="ctrl primary"
				disabled={text.trim().length === 0}
				onclick={() => onSendToAgent(text.trim())}
				title={t('canvas.noWorkflow')}
			>
				{t('canvas.sendToAgent')}
			</button>
		{/if}
	</div>
</div>

<style>
	.composer {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 8px 10px 10px;
		border-top: 1px solid var(--border);
		background: var(--bg-surface);
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.label {
		font-size: 9px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	textarea {
		width: 100%;
		box-sizing: border-box;
		resize: vertical;
		min-height: 58px;
		max-height: 280px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-elevated);
		color: var(--text-primary);
		font: inherit;
		font-size: 12px;
		line-height: 1.4;
		padding: 6px 8px;
	}
	textarea:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 0;
	}
	.controls {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.refs {
		flex: 1 0 100%;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.refs img,
	.refs video,
	.refs audio {
		width: 100%;
		max-height: 180px;
		border-radius: 8px;
		background: black;
		object-fit: contain;
	}
	.ctrl {
		appearance: none;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: var(--text-secondary);
		font: inherit;
		font-size: 11px;
		font-weight: 600;
		padding: 4px 8px;
		cursor: pointer;
		text-decoration: none;
	}
	.ctrl:hover:not(:disabled) {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.ctrl:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.ctrl.primary {
		background: var(--accent);
		border-color: var(--accent);
		color: white;
	}
	.ctrl:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
	}
</style>
