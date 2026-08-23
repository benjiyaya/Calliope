<script lang="ts">
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		/** Persisted reasoning text (from stored message). */
		reasoning?: string | null;
		/** Live-streaming reasoning text (from agent.thinking SSE). */
		streaming?: string;
	}

	let { reasoning = null, streaming = '' }: Props = $props();

	let open = $state(false);

	const hasReasoning = $derived(!!reasoning || !!streaming);
	const display = $derived(streaming || reasoning || '');
	const isLive = $derived(!!streaming);
</script>

{#if hasReasoning}
	<div class="reasoning-panel" class:live={isLive}>
		<button type="button" class="head" onclick={() => (open = !open)} aria-expanded={open}>
			<span class="icon-wrap" class:spinning={isLive && !open}>
				<Icon name="sparkle" size={13} />
			</span>
			<span class="label">
				{#if isLive && !open}
					Thinking…
				{:else if open}
					Reasoning
				{:else}
					Show reasoning
				{/if}
			</span>
			<span class="chev"><Icon name="chevron-down" size={12} /></span>
		</button>
		{#if open}
			<div class="body">
				<div class="content">{display}{#if isLive}<span class="caret"></span>{/if}</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	.reasoning-panel {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		overflow: hidden;
	}
	.reasoning-panel.live {
		border-color: color-mix(in srgb, var(--accent) 30%, transparent);
	}
	.head {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		padding: 6px 10px;
		background: transparent;
		border: none;
		cursor: pointer;
		color: var(--text-muted);
		font-size: 12px;
		text-align: left;
	}
	.head:hover {
		color: var(--text-secondary);
		background: rgba(255, 255, 255, 0.02);
	}
	.icon-wrap {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--text-muted);
	}
	.icon-wrap.spinning {
		animation: spin 1.5s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	.label {
		flex: 1;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.chev {
		display: inline-flex;
		flex-shrink: 0;
		transition: transform 0.15s;
	}
	.head[aria-expanded='true'] .chev {
		transform: rotate(180deg);
	}
	.body {
		border-top: 1px solid var(--border);
		padding: 8px 10px;
	}
	.content {
		font-family: var(--font-mono);
		font-size: 11.5px;
		line-height: 1.6;
		color: var(--text-secondary);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 320px;
		overflow-y: auto;
	}
	.caret {
		display: inline-block;
		width: 6px;
		height: 12px;
		margin-left: 1px;
		background: var(--accent);
		animation: blink 1s step-end infinite;
		vertical-align: text-bottom;
	}
	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
</style>
